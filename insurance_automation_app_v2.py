# app_insurance_automation.py
import os
import io
import json
import base64
import glob
import shutil
from typing import List, Dict, Optional, Tuple

import streamlit as st
import pandas as pd

# PDF処理関連
import PyPDF2
from pdf2image import convert_from_bytes, convert_from_path
from PIL import Image

# Gemini
import google.generativeai as genai


# =========== 設定 ===========

st.set_page_config(
    page_title="保険業務自動化アシスタント",
    layout="wide",
    menu_items={
        "About": "このアプリは保険見積書PDFから情報を抽出し、比較表を作成します。"
    },
)

# CSS（日本語フォント & 右上メニュー/フッター非表示は任意）
st.markdown(
    """
<style>
html, body, [class*="css"] {
  font-family: "Noto Sans JP","Yu Gothic","Meiryo",system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
}
/* 右上メニューとフッターを隠す（任意） */
/* #MainMenu {visibility: hidden;} */
/* footer {visibility: hidden;} */

.main-header {
    font-size: 2.0rem;
    font-weight: bold;
    color: #1f77b4;
    text-align: center;
    margin-bottom: 1rem;
}
.section-header {
    font-size: 1.3rem;
    font-weight: bold;
    color: #ff7f0e;
    margin-top: 1.2rem;
    margin-bottom: .6rem;
}
.success-box {
    padding: .75rem;
    background-color: #d4edda;
    border-left: 4px solid #28a745;
    margin: .6rem 0;
}
.info-box {
    padding: .75rem;
    background-color: #d1ecf1;
    border-left: 4px solid #17a2b8;
    margin: .6rem 0;
}
</style>
""",
    unsafe_allow_html=True,
)

st.markdown('<div class="main-header">🏥 保険業務自動化アシスタント</div>', unsafe_allow_html=True)


# =========== ユーティリティ ===========

def get_secret_api_key() -> Optional[str]:
    """st.secrets優先・環境変数フォールバックで GEMINI_API_KEY を取得"""
    try:
        if "GEMINI_API_KEY" in st.secrets:
            return st.secrets["GEMINI_API_KEY"]
    except Exception:
        pass
    return os.getenv("GEMINI_API_KEY")


def init_gemini(model_name: str = "gemini-1.5-flash"):
    """Geminiクライアント初期化（JSONを返す設定）"""
    api_key = get_secret_api_key()
    if not api_key:
        return None, False, "GEMINI_API_KEY が未設定です（st.secrets または環境変数）。"
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name,
            generation_config={"response_mime_type": "application/json"}
        )
        return model, True, None
    except Exception as e:
        return None, False, f"Gemini初期化エラー: {e}"


def poppler_available() -> bool:
    """poppler（pdftoppm）がPATHにあるか"""
    return shutil.which("pdftoppm") is not None


def get_extraction_fields_from_excel(file, sheet_name_candidates: Tuple[str, ...] = ("顧客情報",)) -> List[str]:
    """
    Excelから列名だけを安全に取得する。
    - 行が0でも列名を返す
    - 指定シートが無ければ先頭シートを使う
    - ヘッダー行がズレていても上位数行をスキャンして自動検出
    """
    try:
        xls = pd.ExcelFile(file)
        # シート決定
        target_sheet = None
        for name in sheet_name_candidates:
            if name in xls.sheet_names:
                target_sheet = name
                break
        if target_sheet is None:
            target_sheet = xls.sheet_names[0]

        # 1行目がヘッダー想定で列名だけ読む
        df_head = pd.read_excel(file, sheet_name=target_sheet, header=0, nrows=0)
        cols = [str(c).strip() for c in df_head.columns]
        cols = [c for c in cols if c and not str(c).startswith("Unnamed")]

        # 列名が空なら上位10行を走査して最適行をヘッダに採用
        if not cols:
            tmp = pd.read_excel(file, sheet_name=target_sheet, header=None, nrows=10)
            best_row = None
            best_count = -1
            for i in range(len(tmp)):
                row = tmp.iloc[i]
                count = sum(isinstance(v, str) and str(v).strip() != "" for v in row.tolist())
                if count > best_count:
                    best_count = count
                    best_row = i
            if best_row is not None:
                df_head = pd.read_excel(file, sheet_name=target_sheet, header=best_row, nrows=0)
                cols = [str(c).strip() for c in df_head.columns]
                cols = [c for c in cols if c and not str(c).startswith("Unnamed")]

        return cols
    except Exception as e:
        st.error(f"抽出項目の取得中にエラー: {e}")
        return []


def safe_append(df: pd.DataFrame, record: Dict) -> pd.DataFrame:
    """df列に合わせて不足分を空文字で埋め、1行追加"""
    row = {col: record.get(col, "") for col in df.columns}
    return pd.concat([df, pd.DataFrame([row])], ignore_index=True)


def read_pdf_text(pdf_bytes: bytes) -> str:
    """PyPDF2でPDFテキスト抽出（画像型PDFは空になり得る）"""
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        pages = []
        for p in reader.pages:
            txt = p.extract_text()
            if txt:
                pages.append(txt)
        return "\n\n".join(pages).strip()
    except Exception:
        return ""


def pdf_to_images(pdf_input) -> List[Image.Image]:
    """
    PDFを画像化：
    - bytes を受け取ったら convert_from_bytes
    - パスを受け取ったら convert_from_path
    ※ poppler が必要
    """
    if isinstance(pdf_input, (bytes, bytearray)):
        return convert_from_bytes(pdf_input)
    return convert_from_path(pdf_input)


def gemini_extract_from_text(model, base_prompt: str, text: str) -> Dict:
    """テキストのみでGeminiに抽出依頼"""
    prompt = base_prompt + "\n\n抽出対象の本文:\n" + text
    resp = model.generate_content(prompt)
    # generation_config で JSON を返す設定なので resp.text は JSON 文字列のはず
    return json.loads(resp.text)


def gemini_extract_from_images(model, base_prompt: str, images: List[Image.Image]) -> Dict:
    """
    画像でGeminiに抽出依頼
    - SDKのparts形式（mime_type + data(bytes)）
    """
    parts = [base_prompt]
    for img in images:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        parts.append({"mime_type": "image/png", "data": buf.getvalue()})
    resp = model.generate_content(parts)
    return json.loads(resp.text)


# =========== アプリ状態初期化 ===========

if "customer_df" not in st.session_state:
    st.session_state["customer_df"] = None

if "site_df" not in st.session_state:
    st.session_state["site_df"] = None

# デフォルトの抽出項目（Excelから読み取れない場合の初期値）
if "extraction_fields" not in st.session_state:
    st.session_state["extraction_fields"] = ["氏名", "生年月日", "保険会社名", "保険期間", "保険金額", "補償内容"]

if "comparison_df" not in st.session_state:
    st.session_state["comparison_df"] = pd.DataFrame(columns=st.session_state["extraction_fields"])

if "auto_process_done" not in st.session_state:
    st.session_state["auto_process_done"] = False


# =========== Gemini 初期化・デバッグ ===========

model, GEMINI_ENABLED, gemini_err = init_gemini("gemini-1.5-flash")

with st.sidebar:
    st.markdown("**Debug**")
    st.write("GEMINI_ENABLED:", GEMINI_ENABLED)
    st.write("poppler available:", poppler_available())
    if gemini_err:
        st.warning(gemini_err)

    st.markdown("**使用可能なモデル一覧（generateContent対応）**")
    try:
        models = genai.list_models() if GEMINI_ENABLED else []
        usable = [m.name for m in models if "generateContent" in getattr(m, "supported_generation_methods", [])]
        for n in usable:
            st.write("-", n)
    except Exception as e:
        st.write(f"モデル一覧取得エラー: {e}")


# =========== UI: 事前ファイル準備 ===========

st.markdown('<div class="section-header">📁 1. 事前ファイル準備</div>', unsafe_allow_html=True)

col1, col2 = st.columns(2)

with col1:
    st.subheader("顧客情報.xlsx")
    customer_info_file = st.file_uploader("顧客情報.xlsx をアップロード", type=["xlsx"], key="customer_file")
    if customer_info_file:
        # 列名だけでも取得できる関数で項目を決定
        extraction_fields = get_extraction_fields_from_excel(customer_info_file, sheet_name_candidates=("顧客情報", "Sheet1"))
        if extraction_fields:
            st.session_state["extraction_fields"] = extraction_fields
            # 顧客情報データ本体の読込（空でもOK）
            try:
                excel_all = pd.read_excel(customer_info_file, sheet_name=None)
                # 優先候補シート
                customer_df_try = None
                for nm in ("顧客情報", "Sheet1"):
                    if nm in excel_all:
                        customer_df_try = excel_all[nm]
                        break
                if customer_df_try is None:
                    first_sheet = list(excel_all.keys())[0]
                    customer_df_try = excel_all[first_sheet]
                # 行が無ければ空DFに列だけ立てる
                if customer_df_try is None or customer_df_try.empty:
                    st.session_state["customer_df"] = pd.DataFrame(columns=extraction_fields)
                else:
                    customer_df_try.columns = [str(c).strip() for c in customer_df_try.columns]
                    st.session_state["customer_df"] = customer_df_try
            except Exception:
                st.session_state["customer_df"] = pd.DataFrame(columns=extraction_fields)

            # 比較表も同じ列で初期化（既存があっても列集合を同期）
            st.session_state["comparison_df"] = pd.DataFrame(columns=extraction_fields)

            st.markdown('<div class="success-box">✅ 顧客情報.xlsx の列名を読み込みました（データ行が無くてもOK）。</div>', unsafe_allow_html=True)
            st.dataframe(st.session_state["customer_df"], use_container_width=True)
            st.markdown("**設定された抽出項目:**")
            st.write(", ".join(st.session_state["extraction_fields"]))
        else:
            st.error("顧客情報.xlsx から列名を取得できませんでした。シート名やヘッダー行をご確認ください。")

with col2:
    st.subheader("見積サイト情報.xlsx")
    quote_site_info_file = st.file_uploader("見積サイト情報.xlsx をアップロード", type=["xlsx"], key="site_file")
    if quote_site_info_file:
        try:
            st.session_state["site_df"] = pd.read_excel(quote_site_info_file)
            st.markdown('<div class="success-box">✅ 見積サイト情報.xlsx を読み込みました。</div>', unsafe_allow_html=True)
            st.dataframe(st.session_state["site_df"], use_container_width=True)
        except Exception as e:
            st.error(f"見積サイト情報の読込エラー: {e}")

# 再表示（顧客情報）
if st.session_state["customer_df"] is not None:
    st.dataframe(st.session_state["customer_df"], use_container_width=True)


# =========== UI: 顧客情報管理（PDF→抽出 / 新規手入力） ===========

st.markdown('<div class="section-header">📋 2. 顧客情報管理</div>', unsafe_allow_html=True)
tab1, tab2 = st.tabs(["既存保険PDFから情報抽出", "新規顧客情報入力"])

with tab1:
    st.subheader("既存保険の見積書PDFから情報を抽出")
    existing_pdf = st.file_uploader("既存保険の見積書PDFをアップロード", type=["pdf"], key="existing_pdf")
    if existing_pdf and st.button("PDFから情報を抽出", key="extract_btn"):
        if not GEMINI_ENABLED:
            st.error("GEMINI_API_KEY が未設定のため、Gemini抽出は実行できません。")
        else:
            with st.spinner("PDFから情報を抽出しています..."):
                try:
                    pdf_bytes = existing_pdf.getvalue()

                    # まずテキスト抽出を試す（速く安定）
                    text = read_pdf_text(pdf_bytes)

                    fields = st.session_state["extraction_fields"]
                    example_values = {
                        "氏名": "山田太郎",
                        "生年月日": "1980年1月1日",
                        "保険会社名": "架空保険株式会社",
                        "保険期間": "2025年10月1日～2026年9月30日",
                        "保険金額": "10,000,000円",
                        "補償内容": "入院日額5,000円"
                    }
                    example_json = {f: example_values.get(f, "") for f in fields}
                    base_prompt = (
                        f"以下の保険見積書から {', '.join(fields)} を抽出し、日本語JSONで返してください。"
                        f"不明は空文字。例: {json.dumps(example_json, ensure_ascii=False)}"
                    )

                    if text:
                        extracted = gemini_extract_from_text(model, base_prompt, text)
                    else:
                        # 画像化して抽出（poppler 必要）
                        if not poppler_available():
                            raise RuntimeError("画像型PDFのためpopplerが必要ですが、環境に見つかりません。packages.txt に 'poppler-utils' を追加してください。")
                        images = pdf_to_images(pdf_bytes)
                        extracted = gemini_extract_from_images(model, base_prompt, images)

                    st.markdown('<div class="success-box">✅ PDFから情報を抽出しました。</div>', unsafe_allow_html=True)
                    st.json(extracted)

                    # 比較表に追記（列が無いものは空文字）
                    st.session_state["comparison_df"] = safe_append(st.session_state["comparison_df"], extracted)

                    # 顧客情報にも最低限を追記（任意）
                    if st.session_state["customer_df"] is None:
                        st.session_state["customer_df"] = pd.DataFrame(columns=fields)
                    st.session_state["customer_df"] = safe_append(st.session_state["customer_df"], extracted)

                    st.success("抽出した情報を顧客情報・比較表に追加しました。")

                except Exception as e:
                    st.error(f"PDF抽出エラー: {e}")

with tab2:
    st.subheader("新規顧客情報を入力")
    with st.form("new_customer_form"):
        cols = st.columns(2)
        name = cols[0].text_input("氏名")
        birth = cols[1].text_input("生年月日（例：1980/1/1）")
        submitted = st.form_submit_button("新規顧客情報を追加")
        if submitted:
            # 現在の抽出項目に合わせて追加
            record = {col: "" for col in st.session_state["extraction_fields"]}
            record["氏名"] = name
            record["生年月日"] = birth
            if st.session_state["customer_df"] is None:
                st.session_state["customer_df"] = pd.DataFrame(columns=st.session_state["extraction_fields"])
            st.session_state["customer_df"] = safe_append(st.session_state["customer_df"], record)
            st.success(f"✅ {name} さんの情報を追加しました。")
            st.dataframe(st.session_state["customer_df"], use_container_width=True)


# =========== UI: 見積書PDFから情報抽出（複数/個別） ===========

st.markdown('<div class="section-header">📄 3. 見積書PDFから情報抽出</div>', unsafe_allow_html=True)
st.markdown('<div class="info-box">💡 複数の見積書PDFをアップロードして一括抽出できます。</div>', unsafe_allow_html=True)

uploaded_files = st.file_uploader(
    "PDFファイルを複数選択してアップロード",
    type=["pdf"],
    accept_multiple_files=True,
    help="Ctrl/Shift で複数選択",
    key="multi_pdfs"
)

if uploaded_files and st.button("選択したPDFを処理", key="process_pdfs"):
    if not GEMINI_ENABLED:
        st.error("GEMINI_API_KEY が未設定のため、Gemini抽出は実行できません。")
    else:
        results = []
        progress = st.progress(0)
        status = st.empty()
        total = len(uploaded_files)

        fields = st.session_state["extraction_fields"]
        example_values = {
            "氏名": "山田太郎",
            "生年月日": "1980年1月1日",
            "保険会社名": "架空保険株式会社",
            "保険期間": "2025年10月1日～2026年9月30日",
            "保険金額": "10,000,000円",
            "補償内容": "入院日額5,000円"
        }
        example_json = {f: example_values.get(f, "") for f in fields}
        base_prompt = (
            f"以下の保険見積書から {', '.join(fields)} を抽出し、日本語JSONで返してください。"
            f"不明は空文字。例: {json.dumps(example_json, ensure_ascii=False)}"
        )

        for i, f in enumerate(uploaded_files, start=1):
            status.text(f"処理中: {f.name} ({i}/{total})")
            try:
                pdf_bytes = f.read()
                text = read_pdf_text(pdf_bytes)
                if text:
                    extracted = gemini_extract_from_text(model, base_prompt, text)
                else:
                    if not poppler_available():
                        raise RuntimeError("画像型PDFのためpopplerが必要ですが、環境に見つかりません。packages.txt に 'poppler-utils' を追加してください。")
                    images = pdf_to_images(pdf_bytes)
                    extracted = gemini_extract_from_images(model, base_prompt, images)
                extracted["ファイル名"] = f.name
                results.append(extracted)

                # 比較表へ
                st.session_state["comparison_df"] = safe_append(st.session_state["comparison_df"], extracted)
                st.success(f"✅ {f.name} の処理完了")
            except Exception as e:
                st.error(f"❌ {f.name} の処理エラー: {e}")
            finally:
                progress.progress(i / total)
        status.text("処理完了")

# 単体アップロード（補助）
st.subheader("個別のPDFファイルをアップロード")
one_pdf = st.file_uploader("見積書PDFをアップロード", type=["pdf"], key="one_pdf")
if one_pdf and st.button("見積書から情報を抽出して比較表に追加", key="extract_one_pdf"):
    if not GEMINI_ENABLED:
        st.error("GEMINI_API_KEY が未設定のため、Gemini抽出は実行できません。")
    else:
        try:
            pdf_bytes = one_pdf.getvalue()
            text = read_pdf_text(pdf_bytes)

            fields = st.session_state["extraction_fields"]
            example_values = {
                "氏名": "山田太郎",
                "生年月日": "1980年1月1日",
                "保険会社名": "架空保険株式会社",
                "保険期間": "2025年10月1日～2026年9月30日",
                "保険金額": "10,000,000円",
                "補償内容": "入院日額5,000円"
            }
            example_json = {f: example_values.get(f, "") for f in fields}
            base_prompt = (
                f"以下の保険見積書から {', '.join(fields)} を抽出し、日本語JSONで返してください。"
                f"不明は空文字。例: {json.dumps(example_json, ensure_ascii=False)}"
            )

            if text:
                extracted = gemini_extract_from_text(model, base_prompt, text)
            else:
                if not poppler_available():
                    raise RuntimeError("画像型PDFのためpopplerが必要ですが、環境に見つかりません。packages.txt に 'poppler-utils' を追加してください。")
                images = pdf_to_images(pdf_bytes)
                extracted = gemini_extract_from_images(model, base_prompt, images)

            st.markdown('<div class="success-box">✅ 見積書から情報を抽出しました。</div>', unsafe_allow_html=True)
            st.json(extracted)

            st.session_state["comparison_df"] = safe_append(st.session_state["comparison_df"], extracted)
        except Exception as e:
            st.error(f"見積書抽出エラー: {e}")


# =========== UI: 見積情報比較表 & ダウンロード ===========

st.markdown('<div class="section-header">📊 4. 見積情報比較表</div>', unsafe_allow_html=True)

if not st.session_state["comparison_df"].empty:
    st.dataframe(st.session_state["comparison_df"], use_container_width=True)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        st.session_state["comparison_df"].to_excel(writer, index=False, sheet_name='見積情報比較表')
    st.download_button(
        label="📥 比較表をExcelでダウンロード",
        data=output.getvalue(),
        file_name="見積情報比較表.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
else:
    st.info("まだ見積情報が追加されていません。上のセクションでPDFをアップロードして抽出してください。")


# =========== フッター ===========

st.markdown("---")
st.markdown("**保険業務自動化アシスタント** | Powered by Gemini & Streamlit")