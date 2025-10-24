# insurance_automation_app_v3.py
import os
import io
import json
import shutil
import pandas as pd
import streamlit as st
import PyPDF2
from pdf2image import convert_from_bytes
from PIL import Image
import google.generativeai as genai
import yaml
from yaml.loader import SafeLoader
import streamlit_authenticator as stauth


# ========== Streamlit 基本設定 ==========
st.set_page_config(page_title="保険業務自動化アシスタント", layout="wide")

st.markdown("""
<style>
html, body, [class*="css"] {
  font-family: "Noto Sans JP","Meiryo","Yu Gothic",sans-serif;
}
.main-header { font-size: 2rem; font-weight: bold; color: #1f77b4; text-align: center; margin-bottom: 1rem; }
.section-header { font-size: 1.3rem; font-weight: bold; color: #ff7f0e; margin-top: 1.5rem; margin-bottom: .6rem; }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-header">🏥 保険業務自動化アシスタント</div>', unsafe_allow_html=True)


# ========== 認証設定の読み込み ==========
try:
    with open('config.yaml') as file:
        config = yaml.load(file, Loader=SafeLoader)

    authenticator = stauth.Authenticate(
        config['credentials'],
        config['cookie']['name'],
        config['cookie']['key'],
        config['cookie']['expiry_days'],
        config['preauthorized']
    )

    name, authentication_status, username = authenticator.login("ログイン", "main")

except Exception as e:
    st.error(f"認証設定の読み込みに失敗しました: {e}")
    st.stop()

# ✅ ← この位置に条件分岐を入れます！
if authentication_status:
    st.sidebar.success(f"ようこそ {name} さん！")
    # ✅ この下に、あなたのアプリ本体のコード（PDF抽出やExcel処理）が続きます。
    # 例えば:
    st.write("ここに保険見積り抽出アプリ本体の処理を記述")

elif authentication_status is False:
    st.error("ユーザー名またはパスワードが間違っています。")

elif authentication_status is None:
    st.warning("ログインしてください。")

# ========== Gemini 初期化 ==========
def get_api_key():
    try:
        return st.secrets["GEMINI_API_KEY"]
    except Exception:
        return os.getenv("GEMINI_API_KEY")

def init_gemini():
    api_key = get_api_key()
    if not api_key:
        return None, False, "GEMINI_API_KEY 未設定"
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.5-flash")
        return model, True, "gemini-2.5-flash"
    except Exception as e:
        return None, False, str(e)


# ========== PDF抽出関数群 ==========
def read_pdf_text(pdf_bytes: bytes) -> str:
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        text = "\n\n".join([p.extract_text() or "" for p in reader.pages])
        return text.strip()
    except Exception:
        return ""


def pdf_to_images(pdf_bytes: bytes):
    return convert_from_bytes(pdf_bytes)


def safe_append(df: pd.DataFrame, record: dict) -> pd.DataFrame:
    new_row = {col: record.get(col, "") for col in df.columns}
    return pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)


# ========== メインアプリ本体 ==========
def run_main_app(username, name):
    model, enabled, info = init_gemini()
    if not enabled:
        st.error(f"Gemini 初期化エラー: {info}")
        return

    st.sidebar.success(f"ようこそ、{name} さん！")
    st.sidebar.write(f"ログインユーザー: {username}")

    # --- 管理者限定：新規ユーザー登録フォーム ---
    if username == "admin":
        st.sidebar.markdown("### 👤 新規ユーザー追加（管理者のみ）")
        new_user = st.sidebar.text_input("ユーザー名")
        new_pass = st.sidebar.text_input("パスワード", type="password")
        if st.sidebar.button("登録"):
            hashed_pw = stauth.Hasher([new_pass]).generate()[0]
            config['credentials']['usernames'][new_user] = {
                'name': new_user,
                'password': hashed_pw
            }
            with open('config.yaml', 'w') as file:
                yaml.dump(config, file, default_flow_style=False)
            st.sidebar.success(f"✅ ユーザー '{new_user}' を追加しました！")

    # --- PDF抽出セクション ---
    st.markdown('<div class="section-header">📄 見積書PDFから情報抽出</div>', unsafe_allow_html=True)

    if "comparison_df" not in st.session_state:
        st.session_state["comparison_df"] = pd.DataFrame(columns=["氏名", "生年月日", "保険会社名", "保険期間", "保険金額", "補償内容"])

    uploaded_pdfs = st.file_uploader("PDFファイルをアップロード（複数可）", type=["pdf"], accept_multiple_files=True)

    if uploaded_pdfs and st.button("抽出開始"):
        progress = st.progress(0)
        total = len(uploaded_pdfs)
        for i, pdf in enumerate(uploaded_pdfs, start=1):
            st.info(f"処理中: {pdf.name} ({i}/{total})")
            try:
                pdf_bytes = pdf.read()
                text = read_pdf_text(pdf_bytes)
                fields = st.session_state["comparison_df"].columns.tolist()
                prompt = (
                    f"以下の保険見積書から {', '.join(fields)} を抽出し、日本語JSONで返してください。"
                    f"不明な項目は空文字で。例: {json.dumps({f: '' for f in fields}, ensure_ascii=False)}"
                )
                if text:
                    response = model.generate_content(prompt + "\n\n" + text)
                else:
                    images = pdf_to_images(pdf_bytes)
                    parts = [prompt]
                    for img in images:
                        buf = io.BytesIO()
                        img.save(buf, format="PNG")
                        parts.append({"mime_type": "image/png", "data": buf.getvalue()})
                    response = model.generate_content(parts)

                data = json.loads(response.text)
                data["ファイル名"] = pdf.name
                st.session_state["comparison_df"] = safe_append(st.session_state["comparison_df"], data)
                st.success(f"✅ {pdf.name} 抽出成功")
            except Exception as e:
                st.error(f"❌ {pdf.name} 抽出エラー: {e}")
            progress.progress(i / total)

    # --- 結果表示 ---
    st.markdown('<div class="section-header">📊 比較結果</div>', unsafe_allow_html=True)
    if not st.session_state["comparison_df"].empty:
        st.dataframe(st.session_state["comparison_df"], use_container_width=True)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            st.session_state["comparison_df"].to_excel(writer, index=False, sheet_name="比較表")
        st.download_button("📥 Excelでダウンロード", output.getvalue(), "見積情報比較表.xlsx")

    st.markdown("---")
    st.caption("Powered by Streamlit × Gemini × Streamlit-Authenticator")


# ========== 認証制御 ==========
if authentication_status:
    run_main_app(username, name)
elif authentication_status is False:
    st.error("ユーザー名またはパスワードが間違っています。")
elif authentication_status is None:
    st.warning("ログインしてください。")
