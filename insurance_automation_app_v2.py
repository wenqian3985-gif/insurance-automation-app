import streamlit as st
import pandas as pd
import os
import json
import io
from PIL import Image
from pdf2image import convert_from_path, convert_from_bytes
import base64
import glob
import sys
import google.generativeai as genai
import shutil

# GEMINI_API_KEY を取得 (Streamlit の st.secrets を優先し、環境変数をフォールバック)
GEMINI_API_KEY = None
try:
    GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY")  # Streamlit Cloud の Secrets から取得
except Exception:
    GEMINI_API_KEY = None

if not GEMINI_API_KEY:
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    GEMINI_ENABLED = True
else:
    GEMINI_ENABLED = False

# モデル初期化（参考用）
model = "gemini-2.5-flash"

st.set_page_config(page_title="保険業務自動化アシスタント", layout="wide")
# ---- セッション状態の初期化（必須）----
if "customer_df" not in st.session_state:
    st.session_state["customer_df"] = None

if "site_df" not in st.session_state:
    st.session_state["site_df"] = None


if "comparison_df" not in st.session_state:
    st.session_state["comparison_df"] = pd.DataFrame(
        columns=["保険会社名", "保険期間", "保険金額", "補償内容"]
    )

if "auto_process_done" not in st.session_state:
    st.session_state["auto_process_done"] = False


# カスタムCSS
st.markdown("""
<style>
    .main-header {
        font-size: 2.5rem;
        font-weight: bold;
        color: #1f77b4;
        text-align: center;
        margin-bottom: 2rem;
    }
    .section-header {
        font-size: 1.5rem;
        font-weight: bold;
        color: #ff7f0e;
        margin-top: 2rem;
        margin-bottom: 1rem;
    }
    .success-box {
        padding: 1rem;
        background-color: #d4edda;
        border-left: 4px solid #28a745;
        margin: 1rem 0;
    }
    .info-box {
        padding: 1rem;
        background-color: #d1ecf1;
        border-left: 4px solid #17a2b8;
        margin: 1rem 0;
    }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-header">🏥 保険業務自動化アシスタント</div>', unsafe_allow_html=True)

# セッション状態の初期化（重複防止のため再チェック）
if "customer_df" not in st.session_state:
    st.session_state["customer_df"] = None
if "site_df" not in st.session_state:
    st.session_state["site_df"] = None
if "comparison_df" not in st.session_state:
    st.session_state["comparison_df"] = pd.DataFrame(columns=["保険会社名", "保険期間", "保険金額", "補償内容"])
if "auto_process_done" not in st.session_state:
    st.session_state["auto_process_done"] = False

if not GEMINI_ENABLED:
    st.warning("環境変数 GEMINI_API_KEY が設定されていません。Gemini API 呼び出しは無効になっています。")

# PDF情報抽出関数
def convert_pdf_to_images(pdf_path_or_bytes):
    """PDFファイル（パスまたはbytes）から画像に変換"""
    try:
        # 引数が bytes なら convert_from_bytes を使う
        if isinstance(pdf_path_or_bytes, (bytes, bytearray)):
            images = convert_from_bytes(pdf_path_or_bytes)
        else:
            # パスを与えられた場合はまず convert_from_path を試す
            images = convert_from_path(pdf_path_or_bytes)
        return images
    except Exception as e:
        # よくある原因は poppler が未インストールであること
        hint = (
            "PDF の変換中にエラーが発生しました。"
            " poppler がインストールされていない可能性があります。"
            " Linux (Debian) では次のコマンドでインストールしてください:\n"
            "  sudo apt-get update && sudo apt-get install -y poppler-utils\n"
            " または devcontainer に poppler が含まれているか確認してください。"
        )
        raise RuntimeError(f"{e}\n\n{hint}")

def extract_insurance_info_with_gemini_vision(images):
    """Gemini Vision APIを使用してPDFから保険情報を抽出"""
    if not GEMINI_ENABLED:
        raise RuntimeError("GEMINI_API_KEY が設定されていないため、Gemini API 呼び出しはできません。")

    messages = [
        {"role": "system", "content": "あなたは保険見積書から情報を抽出するアシスタントです。"}
    ]

    user_content = [
        {
            "type": "text",
            "text": "以下の保険見積書の内容から、保険会社名、保険期間、保険金額、補償内容を抽出してください。抽出した情報はJSON形式で出力してください。"
        },
        {
            "type": "text",
            "text": '例: {"保険会社名": "架空保険株式会社", "保険期間": "2025年10月1日～2026年9月30日", "保険金額": "10,000,000円", "補償内容": "入院日額5,000円"}'
        }
    ]

    for i, image in enumerate(images):
        byte_arr = io.BytesIO()
        image.save(byte_arr, format='PNG')
        encoded_image = base64.b64encode(byte_arr.getvalue()).decode('utf-8')

        user_content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{encoded_image}"
            }
        })

    messages.append({"role": "user", "content": user_content})

    # Google Generative AI Python SDK 呼び出し（genai オブジェクトを使用）
    try:
        response = genai.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"}
        )
    except Exception as e:
        raise RuntimeError(f"Gemini API 呼び出し中にエラーが発生しました: {e}")

    # 応答の取り出し
    try:
        # 応答の形式により取り出し方が変わる可能性があるため安全に抽出
        content = None
        # response がオブジェクトの場合
        if hasattr(response, "choices") and len(response.choices) > 0:
            # choice が message を持つ場合
            choice = response.choices[0]
            if hasattr(choice, "message") and hasattr(choice.message, "content"):
                content = choice.message.content
            else:
                # 辞書形式のとき
                content = getattr(choice, "content", None) or choice.get("content") if isinstance(choice, dict) else None
        elif isinstance(response, dict):
            # dict 形式の場合をカバー
            choices = response.get("choices", [])
            if choices:
                msg = choices[0].get("message") or choices[0]
                content = msg.get("content") if isinstance(msg, dict) else None

        if content is None:
            raise RuntimeError("Gemini API の応答からコンテンツを取得できませんでした。")

        return content
    except Exception as e:
        raise RuntimeError(f"Gemini 応答の解析中にエラーが発生しました: {e}")

def process_pdf_folder(folder_path):
    """指定フォルダ内のすべてのPDFファイルを処理"""
    pdf_files = glob.glob(os.path.join(folder_path, "*.pdf"))
    
    if not pdf_files:
        st.warning(f"フォルダ {folder_path} にPDFファイルが見つかりませんでした。")
        return []
    
    results = []
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    for idx, pdf_file in enumerate(pdf_files):
        status_text.text(f"処理中: {os.path.basename(pdf_file)} ({idx + 1}/{len(pdf_files)})")
        
        try:
            # PDFをバイトで読み込み、convert_from_bytes を使って変換（ファイルパス依存問題を回避）
            with open(pdf_file, "rb") as f:
                pdf_bytes = f.read()
            images = convert_pdf_to_images(pdf_bytes)
            extracted_info_str = extract_insurance_info_with_gemini_vision(images)
            
            if isinstance(extracted_info_str, str) and extracted_info_str.startswith("```json") and extracted_info_str.endswith("```"):
                extracted_info_str = extracted_info_str[len("```json\n"):-len("\n```")]

            extracted_info = json.loads(extracted_info_str) if isinstance(extracted_info_str, str) else extracted_info_str
            extracted_info["ファイル名"] = os.path.basename(pdf_file)
            results.append(extracted_info)
            
        except Exception as e:
            st.error(f"{os.path.basename(pdf_file)} の処理中にエラーが発生しました: {e}")
        
        progress_bar.progress((idx + 1) / len(pdf_files))
    
    status_text.text("すべてのPDFファイルの処理が完了しました！")
    return results

# コマンドライン引数からPDFフォルダパスを取得（PADから起動された場合）
pdf_folder_path = None
if len(sys.argv) > 1:
    pdf_folder_path = sys.argv[1]
    if os.path.isdir(pdf_folder_path) and not st.session_state["auto_process_done"]:
        st.markdown('<div class="info-box">📂 PADから起動されました。指定フォルダ内のPDFファイルを自動処理します。</div>', unsafe_allow_html=True)
        st.write(f"**処理対象フォルダ:** {pdf_folder_path}")
        
        with st.spinner("PDFファイルを処理しています..."):
            results = process_pdf_folder(pdf_folder_path)
        
        if results:
            st.markdown('<div class="success-box">✅ すべてのPDFファイルから情報が抽出されました。</div>', unsafe_allow_html=True)
            
            # 比較表に追加
            for result in results:
                new_quote_data = {
                    "保険会社名": result.get("保険会社名", ""),
                    "保険期間": result.get("保険期間", ""),
                    "保険金額": result.get("保険金額", ""),
                    "補償内容": result.get("補償内容", ""),
                }
                new_quote_row = pd.DataFrame([new_quote_data])
                st.session_state["comparison_df"] = pd.concat([st.session_state["comparison_df"], new_quote_row], ignore_index=True)
            
            st.session_state["auto_process_done"] = True

# --- セクション1: 事前ファイル準備 ---
st.markdown('<div class="section-header">📁 1. 事前ファイル準備</div>', unsafe_allow_html=True)

col1, col2 = st.columns(2)

with col1:
    st.subheader("顧客情報.xlsx")
    customer_info_file = st.file_uploader("顧客情報.xlsx をアップロード", type=["xlsx"], key="customer_file")
    if customer_info_file:
        st.session_state["customer_df"] = pd.read_excel(customer_info_file)
        st.markdown('<div class="success-box">✅ 顧客情報.xlsx が正常に読み込まれました。</div>', unsafe_allow_html=True)
        st.dataframe(st.session_state["customer_df"], use_container_width=True)

with col2:
    st.subheader("見積サイト情報.xlsx")
    quote_site_info_file = st.file_uploader("見積サイト情報.xlsx をアップロード", type=["xlsx"], key="site_file")
    if quote_site_info_file:
        st.session_state["site_df"] = pd.read_excel(quote_site_info_file)
        st.markdown('<div class="success-box">✅ 見積サイト情報.xlsx が正常に読み込まれました。</div>', unsafe_allow_html=True)
        st.dataframe(st.session_state["site_df"], use_container_width=True)

# --- セクション2: 顧客情報入力 / 既存保険PDFからの情報抽出 ---
st.markdown('<div class="section-header">📋 2. 顧客情報管理</div>', unsafe_allow_html=True)

tab1, tab2 = st.tabs(["既存保険PDFから情報抽出", "新規顧客情報入力"])

with tab1:
    st.subheader("既存保険の見積書PDFから情報を抽出")
    existing_insurance_pdf = st.file_uploader("既存保険の見積書PDFをアップロード", type=["pdf"], key="existing_pdf")
    
    if existing_insurance_pdf:
        if st.button("PDFから情報を抽出", key="extract_btn"):
            with st.spinner("PDFから情報を抽出しています...しばらくお待ちください。"):
                try:
                    # 一時ファイルとして保存せずに bytes を直接処理
                    pdf_bytes = existing_insurance_pdf.getvalue()
                    images = convert_pdf_to_images(pdf_bytes)
                    extracted_info_str = extract_insurance_info_with_gemini_vision(images)
                    
                    # JSON文字列をパース
                    if isinstance(extracted_info_str, str) and extracted_info_str.startswith("```json") and extracted_info_str.endswith("```"):
                        extracted_info_str = extracted_info_str[len("```json\n"):-len("\n```")]

                    extracted_info = json.loads(extracted_info_str) if isinstance(extracted_info_str, str) else extracted_info_str
                    st.markdown('<div class="success-box">✅ PDFから情報が正常に抽出されました。</div>', unsafe_allow_html=True)
                    st.json(extracted_info)
                    
                    # 抽出した情報を顧客情報に追加
                    if st.session_state["customer_df"] is None:
                        st.session_state["customer_df"] = pd.DataFrame(columns=["氏名", "年齢", "既存保険会社名", "既存保険期間", "既存保険金額", "既存補償内容"])
                    
                    new_customer_data = {
                        "氏名": "既存顧客（要更新）",
                        "年齢": "不明",
                        "既存保険会社名": extracted_info.get("保険会社名", ""),
                        "既存保険期間": extracted_info.get("保険期間", ""),
                        "既存保険金額": extracted_info.get("保険金額", ""),
                        "既存補償内容": extracted_info.get("補償内容", ""),
                    }
                    new_row_df = pd.DataFrame([new_customer_data])
                    st.session_state["customer_df"] = pd.concat([st.session_state["customer_df"], new_row_df], ignore_index=True)
                    st.success("抽出した情報を顧客情報に追加しました。")
                    st.dataframe(st.session_state["customer_df"], use_container_width=True)

                except Exception as e:
                    st.error(f"PDFからの情報抽出中にエラーが発生しました: {e}")

with tab2:
    st.subheader("新規顧客情報を入力")
    with st.form("new_customer_form"):
        col1, col2 = st.columns(2)
        with col1:
            new_name = st.text_input("氏名")
        with col2:
            new_age = st.number_input("年齢", min_value=0, max_value=120)
        
        submitted = st.form_submit_button("新規顧客情報を追加")
        if submitted:
            if st.session_state["customer_df"] is None:
                st.session_state["customer_df"] = pd.DataFrame(columns=["氏名", "年齢"])
            new_customer_row = pd.DataFrame([{"氏名": new_name, "年齢": new_age}])
            st.session_state["customer_df"] = pd.concat([st.session_state["customer_df"], new_customer_row], ignore_index=True)
            st.success(f"✅ {new_name} さんの情報を追加しました。")
            st.dataframe(st.session_state["customer_df"], use_container_width=True)

# --- セクション3: 見積書PDFから情報抽出 ---
st.markdown('<div class="section-header">📄 3. 見積書PDFから情報抽出</div>', unsafe_allow_html=True)

st.markdown('<div class="info-box">💡 保険会社からダウンロードした見積書PDFをアップロードし、情報を抽出して比較表に追加します。</div>', unsafe_allow_html=True)

# フォルダ指定による一括処理
st.subheader("フォルダ内のPDFファイルを一括処理")
folder_path_input = st.text_input("PDFファイルが保存されているフォルダパスを入力", placeholder="例: /home/yourname/Downloads/見積書")

if folder_path_input and st.button("フォルダ内のすべてのPDFを処理", key="process_folder_btn"):
    if os.path.isdir(folder_path_input):
        with st.spinner("フォルダ内のPDFファイルを処理しています..."):
            results = process_pdf_folder(folder_path_input)
        
        if results:
            st.markdown('<div class="success-box">✅ すべてのPDFファイルから情報が抽出されました。</div>', unsafe_allow_html=True)
            
            # 比較表に追加
            for result in results:
                new_quote_data = {
                    "保険会社名": result.get("保険会社名", ""),
                    "保険期間": result.get("保険期間", ""),
                    "保険金額": result.get("保険金額", ""),
                    "補償内容": result.get("補償内容", ""),
                }
                new_quote_row = pd.DataFrame([new_quote_data])
                st.session_state["comparison_df"] = pd.concat([st.session_state["comparison_df"], new_quote_row], ignore_index=True)
    else:
        st.error("指定されたフォルダが存在しません。正しいパスを入力してください。")

st.markdown("---")

# 個別PDFアップロード
st.subheader("個別のPDFファイルをアップロード")
quote_pdf = st.file_uploader("見積書PDFをアップロード", type=["pdf"], key="quote_pdf")

if quote_pdf:
    if st.button("見積書から情報を抽出して比較表に追加", key="extract_quote_btn"):
        with st.spinner("見積書から情報を抽出しています..."):
            try:
                pdf_bytes = quote_pdf.getvalue()
                images = convert_pdf_to_images(pdf_bytes)
                extracted_info_str = extract_insurance_info_with_gemini_vision(images)
                
                if isinstance(extracted_info_str, str) and extracted_info_str.startswith("```json") and extracted_info_str.endswith("```"):
                    extracted_info_str = extracted_info_str[len("```json\n"):-len("\n```")]

                extracted_info = json.loads(extracted_info_str) if isinstance(extracted_info_str, str) else extracted_info_str
                st.markdown('<div class="success-box">✅ 見積書から情報が正常に抽出されました。</div>', unsafe_allow_html=True)
                st.json(extracted_info)
                
                # 比較表に追加
                new_quote_data = {
                    "保険会社名": extracted_info.get("保険会社名", ""),
                    "保険期間": extracted_info.get("保険期間", ""),
                    "保険金額": extracted_info.get("保険金額", ""),
                    "補償内容": extracted_info.get("補償内容", ""),
                }
                new_quote_row = pd.DataFrame([new_quote_data])
                st.session_state["comparison_df"] = pd.concat([st.session_state["comparison_df"], new_quote_row], ignore_index=True)
                st.success("✅ 抽出した情報を比較表に追加しました。")

            except Exception as e:
                st.error(f"見積書からの情報抽出中にエラーが発生しました: {e}")

# --- セクション4: 見積情報比較表 ---
st.markdown('<div class="section-header">📊 4. 見積情報比較表</div>', unsafe_allow_html=True)

if not st.session_state["comparison_df"].empty:
    st.dataframe(st.session_state["comparison_df"], use_container_width=True)
    
    # Excelダウンロード機能
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        st.session_state["comparison_df"].to_excel(writer, index=False, sheet_name='見積情報比較表')
    excel_data = output.getvalue()
    
    st.download_button(
        label="📥 比較表をExcelでダウンロード",
        data=excel_data,
        file_name="見積情報比較表.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
else:
    st.info("まだ見積情報が追加されていません。セクション3で見積書PDFをアップロードして情報を抽出してください。")

# --- フッター ---
st.markdown("---")
st.markdown("**保険業務自動化アシスタント** | Powered by Gemini & Streamlit")

