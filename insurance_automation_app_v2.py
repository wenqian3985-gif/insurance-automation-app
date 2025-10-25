import os
import streamlit as st
import pandas as pd
import PyPDF2
import io
import json
import google.generativeai as genai
from pdf2image import convert_from_bytes
from PIL import Image
import streamlit_authenticator as stauth
from streamlit_authenticator import Hasher
import time

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
/* 強制リセットボタンのスタイル */
.reset-button button { 
    background-color: #ff4b4b !important;
    border-color: #ff4b4b !important;
    color: white !important;
    font-weight: bold;
}
.stButton>button { border-radius: 8px; border: 1px solid #2ca02c; color: white; background-color: #2ca02c; }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-header">🏥 保険業務自動化アシスタント</div>', unsafe_allow_html=True)


# ======================
# 認証設定の読み込みと初期化
# ======================
try:
    # Secretsから認証設定を読み込む
    if 'auth' not in st.secrets:
        st.error("❌ Secretsに認証設定（[auth]セクション）が見つかりません。")
        st.stop()
        
    config_auth = {
        "credentials": {
            "usernames": st.secrets["auth"]["credentials"]["usernames"]
        },
        "cookie": {
            # Secretsから直接設定値を参照
            "name": st.secrets["auth"]["cookie_name"],
            "key": st.secrets["auth"]["cookie_key"],
            "expiry_days": st.secrets["auth"]["expiry_days"],
        },
        "preauthorized": {"emails": []}
    }
    
    # Authenticateオブジェクトの初期化
    authenticator = stauth.Authenticate(
        config_auth["credentials"],
        config_auth["cookie"]["name"],
        config_auth["cookie"]["key"],
        config_auth["cookie"]["expiry_days"],
        force_update=True
    )
    print("Authentication initialized successfully.")

except Exception as e:
    # 認証初期化失敗時の詳細なログを出力
    print(f"Authentication Initialization Failed: {e}")
    st.error(f"ログイン画面の初期化に失敗しました。Secretsの設定を確認してください。エラー: {e}")
    authenticator = None 
    st.stop() 


# ======================
# ログインフォームと認証
# ======================

# セッション状態の初期化
if "authentication_status" not in st.session_state:
    st.session_state["authentication_status"] = None
    st.session_state["name"] = None
    st.session_state["username"] = None
if "auth_render_error" not in st.session_state:
    st.session_state["auth_render_error"] = False

# 強制リセット関数
def force_session_reset():
    """セッション状態をクリアし、強制的に再実行する"""
    st.session_state["authentication_status"] = None
    st.session_state["name"] = None
    st.session_state["username"] = None
    st.session_state["auth_render_error"] = False
    st.success("セッション状態をリセットしました。アプリケーションが再起動します。")
    time.sleep(1) # メッセージ表示のための短い待機
    st.experimental_rerun()


# authenticatorが初期化されているか確認
if authenticator:
    
    # 【修正: 致命的なレンダリングエラー発生時の処理】
    if st.session_state["auth_render_error"]:
        st.error("❌ 認証フォームのレンダリング中に致命的なエラーが検出されました。")
        st.warning("この問題は、Streamlitのセッション状態が不安定になっていることが原因である可能性があります。")
        st.info("通常の解決策（リロード、キャッシュクリア）で解決しない場合は、以下の**最終手段**をお試しください。")
        
        # 最終手段として、強制リセットボタンを表示
        st.markdown('<div class="reset-button">', unsafe_allow_html=True)
        if st.button("🔴 セッション強制リセット (最終手段)", on_click=force_session_reset):
             pass # on_clickで処理が実行される
        st.markdown('</div>', unsafe_allow_html=True)

        st.stop() # 処理を停止

    # 1. Cookieによる認証状態をチェック
    try:
        name, authentication_status, username = authenticator.cookie_handler()
        
        st.session_state["authentication_status"] = authentication_status
        st.session_state["name"] = name
        st.session_state["username"] = username
        
        if authentication_status is True:
            st.session_state["auth_render_error"] = False

    except Exception as e:
        print(f"Cookie Handler Error (Session Reset): {e}")
        st.session_state["authentication_status"] = False
        st.session_state["name"] = None
        st.session_state["username"] = None


    # 2. 認証ステータスがNoneまたはFalseの場合、ログインフォームを表示
    if st.session_state["authentication_status"] in (None, False):
        
        with st.sidebar:
            st.title("ログイン")
            
            # 認証フォームのレンダリング
            try:
                name, authentication_status, username = authenticator.login(
                    "ログイン", 
                    "sidebar" 
                )
                
                # 認証結果をセッション状態に反映させる
                st.session_state["authentication_status"] = authentication_status
                st.session_state["name"] = name
                st.session_state["username"] = username
                
                st.session_state["auth_render_error"] = False # エラーは発生しなかった

            except Exception as e:
                # 致命的なレンダリングエラーをキャッチした場合はフラグを立てて停止
                st.session_state["auth_render_error"] = True 
                print(f"Login Widget Rendering Error: {e}")
                st.session_state["authentication_status"] = None
                
                # エラーフラグを立てた後、メイン処理 L135 の st.stop() に処理を移す
                st.experimental_rerun()
                
        # 3. 認証後のメッセージ表示ロジック
        if st.session_state["authentication_status"] is False:
            st.sidebar.error("ユーザー名またはパスワードが間違っているか、セッションが期限切れです。")
            st.info("認証が完了するまで、アプリケーションのメイン機能は表示されません。")
            
        elif st.session_state["authentication_status"] is None:
            st.info("認証が完了するまで、アプリケーションのメイン機能は表示されません。")
            st.sidebar.info("ユーザー名とパスワードを入力してください。")


    
    # 4. メインコンテンツの表示 (認証成功時)
    if st.session_state["authentication_status"]:
        st.sidebar.success(f"ようこそ、{st.session_state['name']}さん！")
        authenticator.logout("ログアウト", "sidebar") # ログアウトボタンはサイドバーへ配置

        st.markdown("---")
        st.subheader("📄 保険自動化システム メイン機能")

        # ======================
        # GEMINI 初期化
        # ======================
        try:
            GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel("gemini-2.5-flash")
        except KeyError:
            st.error("❌ GEMINI_API_KEY が設定されていません。Secretsに追加してください。")
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
                        images = convert_pdf_to_images(pdf_bytes)
                        for i, img in enumerate(images[:5]):
                             contents.append(img)
                             if i >= 2: break
                    except Exception as img_e:
                        st.error(f"[{pdf.name}] 画像変換に失敗しました: {img_e}")
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
                    st.error(f"[{pdf.name}] Geminiからの応答をJSONとして解析できませんでした。応答: {response.text[:100]}...")
                    return None
                except Exception as e:
                    st.error(f"[{pdf.name}] Gemini API呼び出しエラー: {e}")
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
        st.markdown("**保険業務自動化アシスタント** | Secure Login + Gemini 2.5 Flash + Streamlit")
    
# 認証オブジェクトが初期化されていない場合は、エラーメッセージを表示したまま停止
elif not authenticator:
    st.error("❌ 認証設定のロードに失敗しました。アプリケーションを起動できません。")
    st.stop()
