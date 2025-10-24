import streamlit as st
import pandas as pd
import PyPDF2
import io
import os
import json
import base64
import shutil
import google.generativeai as genai
from pdf2image import convert_from_bytes
from PIL import Image
import yaml
import streamlit_authenticator as stauth

# ======================
# 基本設定
# ======================
st.set_page_config(page_title="保険業務自動化アシスタント", layout="wide")

st.markdown("""
<style>
html, body, [class*="css"] {
  font-family: "Noto Sans JP","Meiryo","Yu Gothic",sans-serif;
}
.main-header { font-size: 2rem; font-weight: bold; color: #1f77b4; text-align: center; margin-bottom: 1rem; }
.section-header { font-size: 1.3rem; font-weight: bold; color: #ff7f0e; margin-top: 1.5rem; margin-bottom: .6rem; }
.success-box { background:#d4edda; padding:.8rem; border-left:4px solid #28a745; margin:.5rem 0; }
.info-box { background:#d1ecf1; padding:.8rem; border-left:4px solid #17a2b8; margin:.5rem 0; }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-header">🏥 保険業務自動化アシスタント</div>', unsafe_allow_html=True)

# ======================
# 認証設定の読み込み
# ======================
try:
    with open("config.yaml", "r", encoding="utf-8") as file:
        config = yaml.safe_load(file)
except Exception as e:
    st.error(f"認証設定の読み込みに失敗しました: {e}")
    st.stop()

# Streamlit Authenticator 初期化（pre_authorized 削除対応版）
authenticator = stauth.Authenticate(
    credentials=config["credentials"],
    cookie_name=config["cookie"]["name"],
    key=config["cookie"]["key"],
    cookie_expiry_days=config["cookie"]["expiry_days"],
)

# ======================
# ログイン処理
# ======================
try:
    authentication_result = authenticator.login(location="main")

    if authentication_result is None:
        st.error("ログイン画面の初期化に失敗しました。設定を確認してください。")
        st.stop()

    name, authentication_status, username = authentication_result

    if authentication_status is False:
        st.error("ユーザー名またはパスワードが間違っています。")
    elif authentication_status is None:
        st.warning("ユーザー名とパスワードを入力してください。")
    else:
        st.success(f"ようこそ、{name} さん！")
        authenticator.logout("ログアウト", "sidebar")
except Exception as e:
    st.error(f"ログイン画面の初期化に失敗しました: {e}")
    st.stop()

# ======================
# GEMINI 初期化
# ======================
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    st.error("GEMINI_API_KEY が設定されていません。Streamlit Secretsに登録してください。")
    st.stop()

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

# ======================
# 関数定義
# ======================
def extract_text_from_pdf(pdf_bytes):
    """PDFからテキスト抽出"""
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        text = "\n\n".join([p.extract_text() or "" for p in reader.pages])
        return text.strip()
    except Exception:
        return ""

def convert_pdf_to_images(pdf_bytes):
    """PDFを画像リストに変換"""
    return convert_from_bytes(pdf_bytes)

def extract_info_with_gemini(pdf_bytes, fields):
    """Geminiで情報抽出"""
    text = extract_text_from_pdf(pdf_bytes)
    example_json = {f: "" for f in fields}

    prompt = (
        f"以下の保険見積書から {', '.join(fields)} を抽出し、日本語のJSONで返してください。\n"
        f"不明な項目は空文字にしてください。例: {json.dumps(example_json, ensure_ascii=False)}"
    )

    try:
        if text:
            response = model.generate_content(prompt + "\n\n" + text)
        else:
            images = convert_pdf_to_images(pdf_bytes)
            contents = [{"text": prompt}]
            for img in images:
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                contents.append({"mime_type": "image/png", "data": base64.b64encode(buf.getvalue()).decode("utf-8")})
            response = model.generate_content(contents)

        if not response or not response.text:
            raise ValueError("Geminiの応答が空です。")

        clean_text = response.text.strip().strip("```json").strip("```").strip()
        return json.loads(clean_text)
    except Exception as e:
        raise RuntimeError(f"PDF抽出エラー: {e}")

# ======================
# アプリ本体
# ======================
st.markdown('<div class="section-header">📁 1. 顧客情報ファイルをアップロード</div>', unsafe_allow_html=True)

customer_file = st.file_uploader("顧客情報.xlsx をアップロード", type=["xlsx"])
if customer_file:
    df_customer = pd.read_excel(customer_file)
    st.session_state["fields"] = df_customer.columns.tolist()
    st.success("✅ 顧客情報ファイルを読み込みました。")
    st.dataframe(df_customer)
else:
    st.session_state["fields"] = ["氏名", "生年月日", "保険会社名", "保険期間", "保険金額", "補償内容"]

# ======================
# PDF処理セクション
# ======================
st.markdown('<div class="section-header">📄 2. 見積書PDFから情報抽出</div>', unsafe_allow_html=True)
uploaded_pdfs = st.file_uploader("PDFファイルをアップロード（複数可）", type=["pdf"], accept_multiple_files=True)

if uploaded_pdfs and st.button("PDFから情報を抽出"):
    results = []
    fields = st.session_state["fields"]

    for pdf in uploaded_pdfs:
        st.info(f"{pdf.name} を処理中...")
        try:
            pdf_bytes = pdf.read()
            data = extract_info_with_gemini(pdf_bytes, fields)
            data["ファイル名"] = pdf.name
            results.append(data)
            st.success(f"✅ {pdf.name} 抽出成功")
        except Exception as e:
            st.error(str(e))

    if results:
        df = pd.DataFrame(results)
        st.session_state["comparison_df"] = df
        st.dataframe(df, use_container_width=True)
    else:
        st.warning("PDFから情報を抽出できませんでした。")

# ======================
# 結果ダウンロード
# ======================
st.markdown('<div class="section-header">📊 3. 抽出結果をダウンロード</div>', unsafe_allow_html=True)
if "comparison_df" in st.session_state and not st.session_state["comparison_df"].empty:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        st.session_state["comparison_df"].to_excel(writer, index=False, sheet_name="見積情報比較表")
    st.download_button(
        "📥 Excelでダウンロード",
        data=output.getvalue(),
        file_name="見積情報比較表.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
else:
    st.info("まだ抽出結果はありません。")

# ======================
# ログアウトボタン
# ======================
authenticator.logout("ログアウト", "sidebar")

st.markdown("---")
st.markdown("**保険業務自動化アシスタント** | Secure Login + Gemini 2.5 Flash + Streamlit")
