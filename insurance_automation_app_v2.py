# insurance_automation_app_v3.py
import os
import io
import json
import bcrypt
import streamlit as st
import pandas as pd
from pdf2image import convert_from_bytes
from PIL import Image
import PyPDF2
import google.generativeai as genai
import streamlit_authenticator as stauth


# ===============================
# 🔐 認証セクション
# ===============================
try:
    credentials = st.secrets["credentials"]
    cookie = st.secrets["cookie"]
    preauthorized = st.secrets.get("preauthorized", {})

    authenticator = stauth.Authenticate(
        credentials,
        cookie["name"],
        cookie["key"],
        cookie["expiry_days"],
        preauthorized
    )

    name, authentication_status, username = authenticator.login("ログイン", "main")

except Exception as e:
    st.error(f"認証設定に問題があります: {e}")
    st.stop()

if authentication_status is False:
    st.error("ユーザー名またはパスワードが間違っています。")
    st.stop()
elif authentication_status is None:
    st.warning("ログイン情報を入力してください。")
    st.stop()
else:
    authenticator.logout("ログアウト", "sidebar")
    st.sidebar.success(f"ログイン中: {name}")


# ===============================
# 🧠 Gemini モデル設定
# ===============================
def get_api_key():
    if "GEMINI_API_KEY" in st.secrets:
        return st.secrets["GEMINI_API_KEY"]
    return os.getenv("GEMINI_API_KEY")

def init_gemini():
    api_key = get_api_key()
    if not api_key:
        return None, False, "GEMINI_API_KEY が未設定です"
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.5-flash")
        return model, True, "gemini-2.5-flash"
    except Exception as e:
        return None, False, f"初期化エラー: {e}"

model, GEMINI_ENABLED, model_info = init_gemini()


# ===============================
# 🧩 ユーティリティ関数群
# ===============================
def get_fields_from_excel(file):
    try:
        xls = pd.ExcelFile(file)
        df = pd.read_excel(file, sheet_name=xls.sheet_names[0], nrows=0)
        cols = [c for c in df.columns if not str(c).startswith("Unnamed")]
        return [str(c).strip() for c in cols]
    except Exception as e:
        st.error(f"Excel列名取得エラー: {e}")
        return []

def read_pdf_text(pdf_bytes):
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        return "\n\n".join([p.extract_text() or "" for p in reader.pages])
    except Exception:
        return ""

def safe_append(df, record):
    new_row = {col: record.get(col, "") for col in df.columns}
    return pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)


# ===============================
# 🧭 サイドバー（管理者メニュー）
# ===============================
menu = st.sidebar.radio("メニューを選択", ["見積情報抽出", "ユーザー追加（管理者のみ）"])

# ===============================
# 📁 見積情報抽出メインページ
# ===============================
if menu == "見積情報抽出":
    st.title("🏥 保険業務自動化アシスタント")

    if "comparison_df" not in st.session_state:
        st.session_state["comparison_df"] = pd.DataFrame(
            columns=["氏名", "生年月日", "保険会社名", "保険期間", "保険金額", "補償内容"]
        )

    st.header("📁 顧客情報ファイルをアップロード")
    customer_file = st.file_uploader("顧客情報.xlsx をアップロード", type=["xlsx"])
    if customer_file:
        fields = get_fields_from_excel(customer_file)
        if fields:
            st.session_state["comparison_df"] = pd.DataFrame(columns=fields)
            st.success("✅ 列名を取得しました。")
            st.write("抽出対象:", ", ".join(fields))
        else:
            st.error("列名を取得できませんでした。")

    st.header("📄 見積書PDFをアップロードして抽出")
    uploaded_pdfs = st.file_uploader("PDFファイルをアップロード（複数可）", type=["pdf"], accept_multiple_files=True)

    if uploaded_pdfs and st.button("PDFから情報を抽出"):
        if not GEMINI_ENABLED:
            st.error("Gemini API が無効です。")
        else:
            progress = st.progress(0)
            total = len(uploaded_pdfs)
            for i, pdf in enumerate(uploaded_pdfs, start=1):
                st.info(f"処理中: {pdf.name} ({i}/{total})")
                try:
                    pdf_bytes = pdf.read()
                    text = read_pdf_text(pdf_bytes)
                    fields = st.session_state["comparison_df"].columns.tolist()
                    example_json = {f: "" for f in fields}
                    prompt = (
                        f"以下の保険見積書から {', '.join(fields)} を抽出し、日本語JSONで返してください。"
                        f"不明な項目は空文字で。例: {json.dumps(example_json, ensure_ascii=False)}"
                    )
                    response = model.generate_content(prompt + "\n\n" + text)
                    data = json.loads(response.text)
                    data["ファイル名"] = pdf.name
                    st.session_state["comparison_df"] = safe_append(st.session_state["comparison_df"], data)
                    st.success(f"✅ {pdf.name} 抽出成功")
                except Exception as e:
                    st.error(f"❌ {pdf.name} 抽出エラー: {e}")
                progress.progress(i / total)

    st.header("📊 抽出結果")
    if not st.session_state["comparison_df"].empty:
        st.dataframe(st.session_state["comparison_df"], use_container_width=True)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            st.session_state["comparison_df"].to_excel(writer, index=False)
        st.download_button("📥 Excelでダウンロード",
                           data=output.getvalue(),
                           file_name="見積情報比較表.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    else:
        st.info("まだ抽出結果がありません。")


# ===============================
# 🧑‍💻 ユーザー追加ページ（管理者専用）
# ===============================
elif menu == "ユーザー追加（管理者のみ）":
    if username != "admin":
        st.error("⚠️ このページは管理者のみアクセスできます。")
    else:
        st.title("👥 新規ユーザー追加ツール")
        st.write("以下のフォームに入力して、Secrets.toml に追加する内容を生成します。")

        new_user = st.text_input("ユーザー名（例：user2）")
        display_name = st.text_input("表示名（例：田中太郎）")
        password = st.text_input("パスワード", type="password")

        if st.button("ハッシュを生成"):
            if not new_user or not password:
                st.warning("ユーザー名とパスワードを入力してください。")
            else:
                hashed_pw = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
                st.success("✅ 以下を Secrets.toml に追記してください：")
                st.code(f"""
[credentials.usernames.{new_user}]
name = "{display_name or new_user}"
password = "{hashed_pw}"
""", language="toml")
                st.info("👆 上記をコピーして、Streamlit Cloud の Secrets に貼り付けて保存してください。")

st.markdown("---")
st.markdown("**保険業務自動化アシスタント v3 | Secure Access Enabled**")
