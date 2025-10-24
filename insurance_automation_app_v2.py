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
.stButton>button { border-radius: 8px; border: 1px solid #2ca02c; color: white; background-color: #2ca02c; }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-header">🏥 保険業務自動化アシスタント</div>', unsafe_allow_html=True)


# ======================
# 認証設定の読み込みと初期化
# ======================
try:
    # Secretsから認証設定を読み込む
    # Streamlit Cloudでの標準的なSecrets管理方法を採用
    if 'auth' not in st.secrets:
        st.error("❌ Secretsに認証設定（[auth]セクション）が見つかりません。")
        st.stop()
        
    config_auth = {
        "credentials": {
            "usernames": st.secrets["auth"]["credentials"]["usernames"]
        },
        "cookie": {
            "name": st.secrets["auth"]["cookie_name"],
            "key": st.secrets["auth"]["cookie_key"],
            "expiry_days": st.secrets["auth"]["expiry_days"],
        },
        "preauthorized": {"emails": []} # preauthorizedは空でOK
    }

    # Authenticateオブジェクトの初期化
    authenticator = stauth.Authenticate(
        config_auth["credentials"],
        config_auth["cookie"]["name"],
        config_auth["cookie"]["key"],
        config_auth["cookie"]["expiry_days"],
    )
except Exception as e:
    st.error(f"ログイン画面の初期化に失敗しました。Secretsの設定を確認してください。エラー: {e}")
    # st.stop() は、エラー発生時に再実行を妨げ、デバッグを難しくするため、ここでは使用しない
    authenticator = None 


# ======================
# ログインフォームと認証
# ======================

# authenticatorが初期化されているか確認
if authenticator:

# ログイン処理。戻り値のアンパックは3つ
    # 【修正点】streamlit_authenticatorのTypeError回避のため、fields引数ではなく、
    # form_nameを第一引数として渡す、より互換性の高い呼び出し方に変更します。
    name, authentication_status, username = authenticator.login(form_name="ログイン", location="main")
    

    # 認証ステータスに応じた処理
    if authentication_status is False:
        st.error("ユーザー名またはパスワードが間違っています。")
    elif authentication_status is None:
        st.info("ユーザー名とパスワードを入力してください。")
    
    # ログイン成功後の画面
    if authentication_status:
        st.success(f"ようこそ、{name}さん！")
        authenticator.logout("ログアウト", "sidebar") # ログアウトボタンはサイドバーへ移動

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
        # @st.cache_data を使用
        @st.cache_data
        def extract_text_from_pdf(pdf_bytes):
            """PDFからテキスト抽出"""
            try:
                reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
                # ページごとにテキストを結合。抽出失敗時は空文字
                text = "\n\n".join([p.extract_text() or "" for p in reader.pages])
                return text.strip()
            except Exception as e:
                # 抽出エラーをログに残すが、処理は継続
                print(f"PDFテキスト抽出エラー（PyPDF2）: {e}")
                return ""

        @st.cache_data
        def convert_pdf_to_images(pdf_bytes):
            """PDFを画像に変換"""
            # convert_from_bytesは重い処理のため、キャッシュを推奨
            return convert_from_bytes(pdf_bytes)

        # Gemini APIで情報抽出（キャッシュなし）
        def extract_info_with_gemini(pdf_bytes, fields, pdf_name):
            """Gemini APIで情報抽出"""
            
            # 処理状況をユーザーに伝えるためのスピナーを追加
            with st.spinner(f"[{pdf_name}] Geminiによる情報抽出中..."):
                text = extract_text_from_pdf(pdf_bytes)
                example_json = {f: "" for f in fields}

                # プロンプトをより明確にJSON形式を要求するように修正
                prompt = (
                    f"以下の保険見積書（またはその画像）から、指定されたすべての項目を抽出し、"
                    f"**必ず**JSON形式で返してください。不明な項目は空文字にしてください。\n"
                    f"抽出項目リスト: {', '.join(fields)}\n"
                    f"JSON形式の例: {json.dumps(example_json, ensure_ascii=False)}"
                )

                contents = [{"text": prompt}]
                
                # 1. まずはテキスト情報を使用
                if text and len(text) > 100:
                    contents.append({"text": f"--- PDF TEXT START ---\n{text}"})
                else:
                    # 2. テキスト抽出が不十分または失敗した場合（画像として処理）
                    st.warning(f"[{pdf_name}] テキスト抽出が不十分なため、画像として処理します。")
                    try:
                        images = convert_pdf_to_images(pdf_bytes)
                        # 最初の数ページのみを処理してトークン制限を回避
                        for i, img in enumerate(images[:5]):
                             contents.append(img) # PIL Imageオブジェクトを直接渡す
                             if i >= 2: break # 3ページ目までで十分とする
                    except Exception as img_e:
                        st.error(f"[{pdf_name}] 画像変換に失敗しました: {img_e}")
                        return None

                try:
                    # generate_contentの引数を修正: contentsがリストの場合はそのまま渡す
                    response = model.generate_content(contents)

                    if not response or not response.text:
                        raise ValueError("Geminiの応答が空です。")

                    # JSONパースの堅牢性を高めるために、応答からJSONブロックを抽出
                    clean_text = response.text.strip()
                    # MarkdownのJSONブロック（```json ... ```）をクリーンアップ
                    if clean_text.startswith("```"):
                        clean_text = clean_text.replace("```json", "").replace("```", "").strip()
                    
                    return json.loads(clean_text)
                except json.JSONDecodeError:
                    st.error(f"[{pdf_name}] Geminiからの応答をJSONとして解析できませんでした。応答: {response.text[:100]}...")
                    return None
                except Exception as e:
                    # エラーメッセージに元のファイル名を含める
                    st.error(f"[{pdf_name}] Gemini API呼び出しエラー: {e}")
                    return None

        # ======================
        # アプリ本体
        # ======================
        
        # セッションステートの初期化をファイルのアップロード前に移動
        if "fields" not in st.session_state:
            # デフォルトのフィールド設定
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
                
                # 列名を抽出フィールドとして設定
                new_fields = df_customer.columns.tolist()
                st.session_state["fields"] = new_fields
                st.session_state["customer_df"] = df_customer # 顧客データもセッションに保存
                
                st.success("✅ 顧客情報ファイルを読み込み、列名を抽出フィールドとして設定しました。")
                st.dataframe(df_customer, use_container_width=True)

            except Exception as e:
                st.error(f"Excelファイルの読み込みエラー: {e}")
                # エラー時はデフォルト値に戻す
                st.session_state["fields"] = ["氏名", "生年月日", "保険会社名", "保険期間", "保険金額", "補償内容"]
                st.session_state["customer_df"] = pd.DataFrame()
                
        # 抽出フィールドの表示
        st.info(f"現在の抽出フィールド: {', '.join(st.session_state['fields'])}")


        st.markdown('<div class="section-header">📄 2. 見積書PDFから情報抽出</div>', unsafe_allow_html=True)
        uploaded_pdfs = st.file_uploader("PDFファイルをアップロード（複数可）", type=["pdf"], accept_multiple_files=True, key="pdf_uploader")
        
        if uploaded_pdfs and st.button("PDFから情報を抽出", key="extract_button"):
            results = []
            fields = st.session_state["fields"]

            # プログレスバーの追加
            progress_bar = st.progress(0)
            total_pdfs = len(uploaded_pdfs)

            for i, pdf in enumerate(uploaded_pdfs):
                try:
                    # PDFの読み込み
                    pdf_bytes = pdf.read()
                    
                    data = extract_info_with_gemini(pdf_bytes, fields, pdf.name)
                    
                    if data:
                        data["ファイル名"] = pdf.name
                        # 抽出フィールドに含まれないキーを削除
                        cleaned_data = {k: v for k, v in data.items() if k in fields or k == "ファイル名"}
                        results.append(cleaned_data)
                        st.success(f"✅ {pdf.name} 抽出成功")
                    else:
                        st.warning(f"⚠️ {pdf.name} は抽出に失敗したか、無効な結果を返しました。")
                        
                except Exception as e:
                    st.error(f"❌ {pdf.name} 処理中に予期せぬエラー: {str(e)}")
                
                # プログレスバーを更新
                progress_bar.progress((i + 1) / total_pdfs)
            
            progress_bar.empty() # 完了したらプログレスバーを消す

            if results:
                # 抽出結果をDataFrameに変換し、セッションに保存
                df = pd.DataFrame(results)
                # 列順序をfieldsの順序に設定 (ファイル名を末尾に追加)
                column_order = [f for f in fields if f in df.columns] + ["ファイル名"]
                df = df.reindex(columns=column_order)
                
                st.session_state["comparison_df"] = df
                st.dataframe(df, use_container_width=True)
            else:
                st.warning("PDFから情報を抽出できませんでした。処理ログを確認してください。")

        st.markdown('<div class="section-header">📊 3. 抽出結果をダウンロード</div>', unsafe_allow_html=True)
        if not st.session_state["comparison_df"].empty:
            # Excelファイルの書き込みを関数化し、@st.cache_dataでキャッシュ可能にする
            @st.cache_data
            def to_excel_bytes(df):
                output = io.BytesIO()
                # openpyxlエンジンを使用
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
    st.info("認証設定のロードに失敗しました。アプリケーションを起動できません。")
