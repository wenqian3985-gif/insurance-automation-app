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

# PDF関連
import PyPDF2
from pdf2image import convert_from_bytes, convert_from_path
from PIL import Image

# Gemini SDK
import google.generativeai as genai


# ========== 基本設定 ==========
st.set_page_config(
    page_title="保険業務自動化アシスタント",
    layout="wide",
)

st.markdown(
    """
<style>
html, body, [class*="css"] {
  font-family: "Noto Sans JP","Meiryo","Yu Gothic",sans-serif;
}
.main-header { font-size: 2rem; font-weight: bold; color: #1f77b4; text-align: center; margin-bottom: 1rem; }
.section-header { font-size: 1.3rem; font-weight: bold; color: #ff7f0e; margin-top: 1.5rem; margin-bottom: .6rem; }
.success-box { background:#d4edda; padding:.8rem; border-left:4px solid #28a745; margin:.5rem 0; }
.info-box { background:#d1ecf1; padding:.8rem; border-left:4px solid #17a2b8; margin:.5rem 0; }
</style>
""",
    unsafe_allow_html=True,
)

st.markdown('<div class="main-header">🏥 保険業務自動化アシスタント</div>', unsafe_allow_html=True)


# ========== ヘルパー関数群 ==========

def get_api_key() -> Optional[str]:
    """st.secrets または環境変数から GEMINI_API_KEY を取得"""
    try:
        if "GEMINI_API_KEY" in st.secrets:
            return st.secrets["GEMINI_API_KEY"]
    except Exception:
        pass
    return os.getenv("GEMINI_API_KEY")


def poppler_available() -> bool:
    """poppler（pdftoppm）が使えるか"""
    return shutil.which("pdftoppm") is not None


def init_gemini():
    """
    Geminiモデル初期化。
    ・モデルは“必ず”軽量の flash 系に固定（デフォルト：gemini-2.5-flash）
    ・Secrets/環境変数 GEMINI_MODEL があれば上書き可能（ただし pro/exp には落とさない想定）
    """
    api_key = get_api_key()
    if not api_key:
        return None, False, "GEMINI_API_KEY が未設定です"

    # デフォルトは軽量モデルに固定
    default_model = "gemini-2.5-flash"
    try:
        user_model = None
        try:
            user_model = st.secrets.get("GEMINI_MODEL", None)  # optional
        except Exception:
            user_model = None
        user_model = user_model or os.getenv("GEMINI_MODEL")

        model_name = (user_model or default_model)

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name)
        return model, True, model_name
    except Exception as e:
        return None, False, f"初期化エラー: {e}"


def get_fields_from_excel(file, sheet_name_candidates=("顧客情報", "Sheet1")) -> List[str]:
    """Excelファイルから列名だけを安全に抽出"""
    try:
        xls = pd.ExcelFile(file)
        # シート選択
        target = next((s for s in sheet_name_candidates if s in xls.sheet_names), xls.sheet_names[0])
        # 最初に列を読む
        df = pd.read_excel(file, sheet_name=target, nrows=0)
        cols = [c for c in df.columns if not str(c).startswith("Unnamed")]
        if not cols:
            tmp = pd.read_excel(file, sheet_name=target, header=None, nrows=5)
            best_row = tmp.apply(lambda r: r.notnull().sum(), axis=1).idxmax()
            df = pd.read_excel(file, sheet_name=target, header=best_row, nrows=0)
            cols = [c for c in df.columns if not str(c).startswith("Unnamed")]
        return [str(c).strip() for c in cols]
    except Exception as e:
        st.error(f"Excel列名取得エラー: {e}")
        return []


def read_pdf_text(pdf_bytes: bytes) -> str:
    """PyPDF2でPDFテキストを抽出"""
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        text = "\n\n".join([p.extract_text() or "" for p in reader.pages])
        return text.strip()
    except Exception:
        return ""


def pdf_to_images(pdf_bytes: bytes, max_pages: int = 2) -> List[Image.Image]:
    """PDF→画像変換（トークン節約のため先頭max_pages枚まで）"""
    images = convert_from_bytes(pdf_bytes)
    if max_pages and len(images) > max_pages:
        images = images[:max_pages]
    return images


def safe_append(df: pd.DataFrame, record: Dict) -> pd.DataFrame:
    """DataFrameへ1行安全追加"""
    new_row = {col: record.get(col, "") for col in df.columns}
    return pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)


def extract_json_string_from_response(response) -> str:
    """
    ★ 修正版の抽出処理部分（2.5向け）
    - response.text が空でも candidates[].content.parts から取り出す
    - ```json ... ``` のコードブロックを除去
    - 文字列を返す（空なら ""）
    """
    raw = ""

    # 1) まず response.text を優先
    if hasattr(response, "text") and isinstance(response.text, str) and response.text.strip():
        raw = response.text.strip()

    # 2) 空なら candidates → parts を探索
    if not raw and hasattr(response, "candidates") and response.candidates:
        parts = getattr(response.candidates[0].content, "parts", None)
        if parts:
            # 最初のテキストパートを優先
            for p in parts:
                text = getattr(p, "text", None)
                if isinstance(text, str) and text.strip():
                    raw = text.strip()
                    break

    # 3) コードブロック除去（```json ... ``` or ``` ... ```）
    if raw.startswith("```"):
        # 先頭の```json or ``` を取り除く
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        # 末尾の ``` を取り除く
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    return raw


# ========== 初期化 ==========

model, GEMINI_ENABLED, model_info = init_gemini()

if "comparison_df" not in st.session_state:
    st.session_state["comparison_df"] = pd.DataFrame(
        columns=["氏名", "生年月日", "保険会社名", "保険期間", "保険金額", "補償内容"]
    )
if "extraction_fields" not in st.session_state:
    st.session_state["extraction_fields"] = st.session_state["comparison_df"].columns.tolist()

# Sidebar Debug
st.sidebar.markdown("**Debug情報**")
st.sidebar.write("GEMINI_ENABLED:", GEMINI_ENABLED)
st.sidebar.write("使用モデル:", model_info)
st.sidebar.write("poppler available:", poppler_available())
if GEMINI_ENABLED:
    try:
        models = genai.list_models()
        st.sidebar.markdown("**利用可能モデル（generateContent対応）**")
        for m in models:
            if "generateContent" in getattr(m, "supported_generation_methods", []):
                st.sidebar.write("-", m.name)
    except Exception as e:
        st.sidebar.write("モデル一覧取得エラー:", e)

if not GEMINI_ENABLED:
    st.error("GEMINI_API_KEY が設定されていないため、Gemini連携は動作しません。")


# ========== セクション1: 顧客情報.xlsx ==========
st.markdown('<div class="section-header">📁 1. 事前ファイル準備</div>', unsafe_allow_html=True)
customer_file = st.file_uploader("顧客情報.xlsx をアップロード", type=["xlsx"])
if customer_file:
    fields = get_fields_from_excel(customer_file)
    if fields:
        st.session_state["extraction_fields"] = fields
        st.session_state["comparison_df"] = pd.DataFrame(columns=fields)
        st.success("✅ 列名を正常に取得しました。")
        st.write("抽出対象:", ", ".join(fields))
    else:
        st.error("列名を取得できませんでした。")


# ========== セクション2: PDFアップロード & 抽出 ==========
st.markdown('<div class="section-header">📄 2. 見積書PDFから情報抽出</div>', unsafe_allow_html=True)
uploaded_pdfs = st.file_uploader("PDFファイルをアップロード（複数可）", type=["pdf"], accept_multiple_files=True)

if uploaded_pdfs and st.button("PDFから情報を抽出"):
    if not GEMINI_ENABLED:
        st.error("Gemini API が無効です。")
    else:
        progress = st.progress(0)
        total = len(uploaded_pdfs)
        results = []

        for i, pdf in enumerate(uploaded_pdfs, start=1):
            st.info(f"処理中: {pdf.name} ({i}/{total})")
            try:
                pdf_bytes = pdf.read()
                text = read_pdf_text(pdf_bytes)

                # 抽出項目設定
                fields = st.session_state["extraction_fields"]
                # 空文字例（不明は空文字で返させる）
                example_json = {f: "" for f in fields}
                prompt = (
                    f"以下の保険見積書から {', '.join(fields)} を抽出し、日本語の有効なJSON（オブジェクト）で返してください。"
                    f"不明な項目は空文字にしてください。例: {json.dumps(example_json, ensure_ascii=False)}"
                )

                # --- API呼び出し直前のモデル指定は init_gemini() で flash 固定済み ---
                # 画像送信はトークン節約のため先頭2ページまで
                if text:
                    response = model.generate_content(prompt + "\n\n" + text)
                else:
                    if not poppler_available():
                        raise RuntimeError("poppler-utils が必要です。packages.txt に追加してください。")
                    images = pdf_to_images(pdf_bytes, max_pages=2)
                    parts = [prompt]
                    for img in images:
                        buf = io.BytesIO()
                        img.save(buf, format="PNG")
                        parts.append({"mime_type": "image/png", "data": buf.getvalue()})
                    response = model.generate_content(parts)

                # ★ 修正版の抽出処理部分（安全な取り出し・コードブロック除去）
                raw_str = extract_json_string_from_response(response)
                if not raw_str:
                    raise ValueError("Geminiから空の応答を受け取りました。PDF内容やサイズをご確認ください。")

                try:
                    data = json.loads(raw_str)
                except json.JSONDecodeError as je:
                    st.sidebar.error(f"[{pdf.name}] JSONデコードエラー: {je}")
                    st.sidebar.code(raw_str, language="json")
                    raise ValueError("JSONのパースに失敗しました。応答形式をご確認ください。")

                # ファイル名付与＆格納
                if isinstance(data, dict):
                    data["ファイル名"] = pdf.name
                    results.append(data)
                    st.success(f"✅ {pdf.name} 抽出成功")
                    st.session_state["comparison_df"] = safe_append(st.session_state["comparison_df"], data)
                else:
                    raise ValueError("Geminiの応答がJSONオブジェクトではありません。")

            except Exception as e:
                st.error(f"❌ {pdf.name} 抽出エラー: {e}")
            progress.progress(i / total)


# ========== セクション3: 結果表示・ダウンロード ==========
st.markdown('<div class="section-header">📊 3. 見積情報比較表</div>', unsafe_allow_html=True)
if not st.session_state["comparison_df"].empty:
    st.dataframe(st.session_state["comparison_df"], use_container_width=True)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        st.session_state["comparison_df"].to_excel(writer, index=False, sheet_name="見積情報比較表")
    st.download_button(
        "📥 Excelでダウンロード",
        data=output.getvalue(),
        file_name="見積情報比較表.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
else:
    st.info("まだPDFから抽出された情報はありません。")

st.markdown("---")
st.markdown("**保険業務自動化アシスタント** | Powered by Streamlit × Gemini")
