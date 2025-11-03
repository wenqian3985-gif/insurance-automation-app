import os
import streamlit as st
import pandas as pd
import PyPDF2
import io
import json
import google.generativeai as genai
from pdf2image import convert_from_bytes
from PIL import Image
import time
# パスワードのハッシュ化処理は使用しません (平文パスワードを使用)

# ======================
# 環境設定・デザイン
# ======================
st.set_page_config(page_title="保険業務自動化アシスタント", layout="wide")

# Noto Sans JPを優先するCSS設定
st.markdown("""
<style>
html, body, [class*="css"] {
    font-family: "Noto Sans JP", "Meiryo", "Yu Gothic", sans-serif;
}
.main-header { font-size: 2.2rem; font-weight: 800; color: #1f77b4; text-align: center; margin-bottom: 1.5rem; }
.section-header { font-size: 1.4rem; font-weight: bold; color: #2ca02c; margin-top: 1.5rem; margin-bottom: 0.8rem; border-bottom: 2px solid #ddd; padding-bottom: 5px; }
/* ログインボタンのスタイル */
.stButton>button { border-radius: 8px; border: 1px solid #2ca02c; color: white; background-color: #2ca02c; }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-header">🏥 保険業務自動化アシスタント</div>', unsafe_allow_html=True)


# ======================
# ネイティブ認証ロジック
# ======================

# セッション状態の初期化
if "authentication_status" not in st.session_state:
    st.session_state["authentication_status"] = None
if "name" not in st.session_state:
    st.session_state["name"] = None

# Secretsからユーザーリストを読み込み、認証用の辞書に変換する
def load_and_map_secrets():
    try:
        # secrets.tomlの [auth_users] users = [...] リストを取得
        user_list = st.secrets["auth_users"]["users"]
        # 認証ロジックで使いやすいよう、{'username': {data}} の辞書に変換
        mapped_users = {user['username']: user for user in user_list}
        if not mapped_users:
             st.error("❌ Secretsファイルにユーザー情報が定義されていません。")
             st.stop()
        return mapped_users
    except KeyError:
        # Secretsのキーが見つからない場合の処理
        st.error("❌ Secretsファイルから認証情報 (`auth_users` または `users`) を読み込めませんでした。`.streamlit/secrets.toml`の構造を確認してください。")
        st.session_state["authentication_status"] = False
        st.stop()
        return {}

# 認証情報辞書のロード (アプリケーション起動時に一度実行)
AUTHENTICATION_USERS = load_and_map_secrets()

def authenticate_user(username, password):
    """ユーザー名と平文パスワードを検証する"""
    
    # 認証用辞書 (AUTHENTICATION_USERS) と照合
    if username in AUTHENTICATION_USERS:
        stored_password = AUTHENTICATION_USERS[username]["password"]
        
        if password == stored_password:
            st.session_state["authentication_status"] = True
            st.session_state["name"] = AUTHENTICATION_USERS[username]["name"]
            st.session_state["username"] = username
            return True
        # else: パスワード不一致 -> 失敗
    # else: ユーザー名が見つからない -> 失敗
    
    st.session_state["authentication_status"] = False
    st.session_state["name"] = None
    st.session_state["username"] = None
    return False

def logout():
    """ログアウト処理"""
    st.session_state["authentication_status"] = None
    st.session_state["name"] = None
    st.session_state["username"] = None
    st.info("ログアウトしました。")
    time.sleep(1)
    st.rerun()
    
# ======================
# ログインフォーム表示
# ======================

if st.session_state["authentication_status"] is not True:
    with st.sidebar:
        st.title("ログイン")
        
        # ログインフォーム
        username_input = st.text_input("ユーザー名")
        password_input = st.text_input("パスワード", type="password")
        
        if st.button("ログイン"):
            if authenticate_user(username_input, password_input):
                st.success("ログイン成功！")
                st.rerun()
            else:
                st.error("ユーザー名またはパスワードが間違っています。")
        
        st.info("認証情報は `.streamlit/secrets.toml` から読み込まれています。")
        st.info("認証が完了するまで、アプリケーションのメイン機能は表示されません。")
else:
    # ログイン成功時のサイドバー表示
    with st.sidebar:
        st.success(f"ようこそ、{st.session_state['name']}さん！")
        if st.button("ログアウト"):
            logout()

# ======================
# メインコンテンツの表示 (認証成功時)
# ======================
if st.session_state["authentication_status"]:

    st.markdown("---")
    st.subheader("📄 保険自動化システム メイン機能")

    # ======================
    # GEMINI 初期化 (SecretsからAPIキーを使用)
    # ======================
    try:
        # SecretsファイルからAPIキーを読み込む
        GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
        
        if not GEMINI_API_KEY:
            st.error("❌ Secretsファイルに `GEMINI_API_KEY` が設定されていません。")
            st.stop()
             
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.5-flash")
    except KeyError:
        st.error("❌ SecretsファイルからAPIキーを読み込めませんでした。`GEMINI_API_KEY`キーを確認してください。")
        st.stop()
    except Exception as e:
        st.error(f"❌ Gemini初期化エラー: {e}")
        st.stop()


    # ======================
    # PDF抽出関数 (堅牢性向上)
    # ======================
    @st.cache_data
    def extract_text_from_pdf(pdf_bytes):
        """PDFからテキスト抽出"""
        try:
            reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
            text = "\n\n".join([p.extract_text() or "" for p in reader.pages])
            return text.strip()
        except Exception as e:
            print(f"PDFテキスト抽出エラー（PyPDF2）: {e}")
            return ""

    @st.cache_data
    def convert_pdf_to_images(pdf_bytes):
        """PDFを画像に変換"""
        return convert_from_bytes(pdf_bytes)

    # Gemini APIで情報抽出（キャッシュなし）
    def extract_info_with_gemini(pdf_bytes, fields, pdf_name):
        """Gemini APIで情報抽出"""
        
        with st.spinner(f"[{pdf_name}] Geminiによる情報抽出中..."):
            text = extract_text_from_pdf(pdf_bytes)
            example_json = {f: "" for f in fields}

            prompt = (
                f"以下の保険見積書（またはその画像）から、指定されたすべての項目を抽出出し、"
                f"**必ず**JSON形式で返してください。不明な項目は空文字にしてください。\n"
                f"抽出項目リスト: {', '.join(fields)}\n"
                f"JSON形式の例: {json.dumps(example_json, ensure_ascii=False)}"
            )

            contents = [{"text": prompt}]
            
            if text and len(text) > 100:
                contents.append({"text": f"--- PDF TEXT START ---\n{text}"})
            else:
                st.warning(f"[{pdf_name}] テキスト抽出が不十分なため、画像として処理します。")
                try:
                    images = convert_from_bytes(pdf_bytes)
                    for i, img in enumerate(images[:5]):
                            contents.append(img)
                            if i >= 2: break
                except Exception as img_e:
                    st.error(f"[{pdf_name}] 画像変換に失敗しました: {img_e}")
                    return None

            try:
                response = model.generate_content(contents)

                if not response or not response.text:
                    raise ValueError("Geminiの応答が空です。")

                clean_text = response.text.strip()
                if clean_text.startswith("```"):
                    clean_text = clean_text.replace("```json", "").replace("```", "").strip()
                
                return json.loads(clean_text)
            except json.JSONDecodeError:
                # 応答がJSONではない場合
                st.error(f"[{pdf_name}] Geminiからの応答をJSONとして解析できませんでした。応答: {response.text[:100]}...")
                return None
            except Exception as e:
                # その他のAPI呼び出しエラー
                st.error(f"[{pdf_name}] Gemini API呼び出しエラー: {e}")
                return None

    # ======================
    # アプリ本体
    # ======================
    
    if "fields" not in st.session_state:
        st.session_state["fields"] = ["氏名", "生年月日", "保険会社名", "保険期間", "保険金額", "補償内容"]
    if "customer_df" not in st.session_state:
        st.session_state["customer_df"] = pd.DataFrame()
    if "comparison_df" not in st.session_state:
        st.session_state["comparison_df"] = pd.DataFrame()


    st.markdown('<div class="section-header">📁 1. 顧客情報ファイルをアップロード (任意)</div>', unsafe_allow_html=True)
    customer_file = st.file_uploader("顧客情報.xlsx をアップロード", type=["xlsx"], key="customer_uploader")
    
    if customer_file:
        try:
            df_customer = pd.read_excel(customer_file)
            new_fields = df_customer.columns.tolist()
            st.session_state["fields"] = new_fields
            st.session_state["customer_df"] = df_customer 
            
            st.success("✅ 顧客情報ファイルを読み込み、列名を抽出フィールドとして設定しました。")
            st.dataframe(df_customer, use_container_width=True)

        except Exception as e:
            st.error(f"Excelファイルの読み込みエラー: {e}")
            st.session_state["fields"] = ["氏名", "生年月日", "保険会社名", "保険期間", "保険金額", "補償内容"]
            st.session_state["customer_df"] = pd.DataFrame()
            
    st.info(f"現在の抽出フィールド: {', '.join(st.session_state['fields'])}")


    st.markdown('<div class="section-header">📄 2. 見積書PDFから情報抽出</div>', unsafe_allow_html=True)
    uploaded_pdfs = st.file_uploader("PDFファイルをアップロード（複数可）", type=["pdf"], accept_multiple_files=True, key="pdf_uploader")
    
    if uploaded_pdfs and st.button("PDFから情報を抽出", key="extract_button"):
        results = []
        fields = st.session_state["fields"]

        progress_bar = st.progress(0)
        total_pdfs = len(uploaded_pdfs)

        for i, pdf in enumerate(uploaded_pdfs):
            try:
                pdf_bytes = pdf.read()
                data = extract_info_with_gemini(pdf_bytes, fields, pdf.name)
                
                if data:
                    data["ファイル名"] = pdf.name
                    # 抽出されたキーが fields に存在するか、または "ファイル名" の場合にのみ残す
                    cleaned_data = {k: v for k, v in data.items() if k in fields or k == "ファイル名"}
                    results.append(cleaned_data)
                    st.success(f"✅ {pdf.name} 抽出成功")
                else:
                    st.warning(f"⚠️ {pdf.name} は抽出に失敗したか、無効な結果を返しました。")
                    
            except Exception as e:
                st.error(f"❌ {pdf.name} 処理中に予期せぬエラー: {str(e)}")
            
            progress_bar.progress((i + 1) / total_pdfs)
        
        progress_bar.empty()

        if results:
            df = pd.DataFrame(results)
            # 列の順序を設定: 抽出フィールド + ファイル名
            column_order = [f for f in fields if f in df.columns] + ["ファイル名"]
            df = df.reindex(columns=column_order)
            
            st.session_state["comparison_df"] = df
            st.dataframe(df, use_container_width=True)
        else:
            st.warning("PDFから情報を抽出できませんでした。処理ログを確認してください。")

    st.markdown('<div class="section-header">📊 3. 抽出結果をダウンロード</div>', unsafe_allow_html=True)
    if not st.session_state["comparison_df"].empty:
        @st.cache_data
        def to_excel_bytes(df):
            output = io.BytesIO()
            # ExcelWriterのエンジンは "openpyxl"
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name="見積情報比較表")
            return output.getvalue()

        excel_data = to_excel_bytes(st.session_state["comparison_df"])
        
        st.download_button(
            "📥 Excelでダウンロード",
            data=excel_data,
            file_name="見積情報比較表_抽出結果.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    else:
        st.info("まだ抽出結果はありません。")

    st.markdown("---")
    st.markdown("**保険業務自動化アシスタント** | Streamlit + Gemini 2.5 Flash")
