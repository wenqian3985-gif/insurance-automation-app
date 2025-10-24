# app_insurance_automation_auth.py

import os
import io
import json
import base64
import glob
import shutil
from typing import List, Dict, Optional

import streamlit as st
import pandas as pd
from PIL import Image
import PyPDF2
from pdf2image import convert_from_bytes
import google.generativeai as genai
import streamlit_authenticator as stauth
import yaml

# ==========================================================
# 🔐 認証設定
# ==========================================================

st.set_page_config(page_title="保険業務自動化アシスタント", layout="wide")

# --- 認証 ---
try:
    authenticator = stauth.Authenticate(
        credentials=st.secrets["credentials"],
        cookie_name=st.secrets["cookie"]["name"],
        key=st.secrets["cookie"]["key"],
        cookie_expiry_days=st.secrets["cookie"]["expiry_days"]
    )
except Exception as e:
    st.error(f"認証設定に問題があります: {e}")
    st.stop()

name, auth_status, username = authenticator.login("ログイン", "main")

if auth_status is False:
    st.error("ユーザー名またはパスワードが違います。")
    st.stop()
elif auth_status is None:
    st.warning("ログインしてください。")
    st.stop()

authenticator.logout("ログアウト", "sidebar")
st.sidebar.success(f"{name} さんでログイン中")

# ==========================================================
# 👤 管理者専用ページ：ユーザー追加フォーム
# ==========================================================

if username == "admin":
    with st.sidebar.expander("⚙️ 管理者メニュー"):
        if st.button("🧑‍💼 ユーザー追加ページを開く"):
            st.session_state["show_user_manager"] = True

if st.session_state.get("show_user_manager"):
    st.title("👤 ユーザー管理（管理者専用）")
    new_user = st.text_input("ユーザー名")
    new_name = st.text_input("表示名")
    new_pass = st.text_input("パスワード", type="password")

    if st.button("登録用YAMLを生成"):
        if new_user and new_pass:
            hashed = stauth.Hasher([new_pass]).generate()[0]
            new_yaml = {
                "usernames": {
                    new_user: {"name": new_name, "password": hashed}
                }
            }
            yaml_str = yaml.dump(new_yaml, allow_unicode=True, sort_keys=False)
            st.code(yaml_str, language="yaml")
            st.success("✅ 上記YAMLをSecretsの`credentials.usernames`下に追加してください。")
        else:
            st.warning("ユーザー名とパスワードを入力してください。")

    st.stop()

# ==========================================================
# 🤖 Gemini 初期化
# ==========================================================

def get_api_key() -> Optional[str]:
    try:
        if "GEMINI_API_KEY" in st.secrets:
            return st.secrets["GEMINI_API_KEY"]
    except Exception:
        pass
    return os.getenv("GEMINI_API_KEY")

api_key = get_api_key()
if not api_key:
    st.error("GEMINI_API_KEY が設定されていません。")
    st.stop()

genai.configure(api_key=api_key)
model = genai.GenerativeModel("gemini-2.5-flash")

# ==========================================================
# 🧰 共通関数
# ==========================================================

def read_pdf_text(pdf_bytes: bytes) -> str:
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        text = "\n\n".join([p.extract_text() or "" for p in reader.pages])
        return text.strip()
    except Exception:
        return ""

def pdf_to_images(pdf_bytes: bytes) -> List[Image.Image]:
    return convert_from_bytes(pdf_bytes)

def safe_append(df: pd.DataFrame, record: Dict) -> pd.DataFrame:
    new_row = {col: record.get(col, "") for col in df.columns}
    return pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)

# ==========================================================
# 🏥 アプリ本体：保険業務自動化
# ==========================================================

st.markdown('<h2 style="color:#1f77b4;">🏥 保険業務自動化アシスタント</h2>', unsafe_allow_html=True)

if "comparison_df" not in st.session_state:
    st.session_state["comparison_df"] = pd.DataFrame(
        columns=["氏名", "生年月日", "保険会社名", "保険期間", "保険金額", "補償内容"]
    )

# --- 顧客情報.xlsx アップロード ---
st.header("📁 顧客情報アップロード")
customer_file = st.file_uploader("顧客情報.xlsx をアップロード", type=["xlsx"])
if customer_file:
    try:
        df = pd.read_excel(customer_file)
        st.session_state["extraction_fields"] = df.columns.tolist()
        st.session_state["comparison_df"] = pd.DataFrame(columns=df.columns.tolist())
        st.success("✅ 顧客情報を読み込みました。")
        st.dataframe(df, use_container_width=True)
    except Exception as e:
        st.error(f"Excel読み込みエラー: {e}")

# --- PDF情報抽出 ---
st.header("📄 保険見積書PDFから情報抽出")
uploaded_pdfs = st.file_uploader("PDFファイルをアップロード（複数可）", type=["pdf"], accept_multiple_files=True)

if uploaded_pdfs and st.button("情報を抽出"):
    results = []
    progress = st.progress(0)
    for i, pdf in enumerate(uploaded_pdfs, start=1):
        st.info(f"{pdf.name} を処理中 ({i}/{len(uploaded_pdfs)})")
        try:
            pdf_bytes = pdf.read()
            text = read_pdf_text(pdf_bytes)
            fields = st.session_state.get("extraction_fields", ["氏名", "生年月日", "保険会社名", "保険期間", "保険金額", "補償内容"])
            example_json = {f: "" for f in fields}
            prompt = (
                f"以下の保険見積書から {', '.join(fields)} を抽出し、日本語JSONで返してください。"
                f"不明な項目は空文字で。例: {json.dumps(example_json, ensure_ascii=False)}"
            )
            response = model.generate_content(prompt + "\n\n" + text)
            data = json.loads(response.text)
            data["ファイル名"] = pdf.name
            results.append(data)
            st.session_state["comparison_df"] = safe_append(st.session_state["comparison_df"], data)
            st.success(f"✅ {pdf.name} の情報を抽出しました。")
        except Exception as e:
            st.error(f"❌ {pdf.name} の抽出エラー: {e}")
        progress.progress(i / len(uploaded_pdfs))

# --- 比較表表示 ---
st.header("📊 見積情報比較表")
if not st.session_state["comparison_df"].empty:
    st.dataframe(st.session_state["comparison_df"], use_container_width=True)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        st.session_state["comparison_df"].to_excel(writer, index=False, sheet_name="比較表")
    st.download_button(
        "📥 Excelでダウンロード",
        data=output.getvalue(),
        file_name="見積情報比較表.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
else:
    st.info("PDFをアップロードして情報を抽出してください。")

st.markdown("---")
st.markdown("**保険業務自動化アシスタント** | Secure Access by Streamlit Authenticator")
