import os
import streamlit as st
import pandas as pd
import PyPDF2
import io
import json
import base64
import google.generativeai as genai
from pdf2image import convert_from_bytes
from PIL import Image
import yaml
import streamlit_authenticator as stauth

# ======================
# 環境設定・デザイン
# ======================
# 環境変数設定はStreamlit Cloudでは不要な場合が多いが、ローカルでの動作のため残す
# st.set_page_configは最初のStreamlitコマンドであるべき
st.set_page_config(page_title="保険業務自動化アシスタント", layout="wide")
os.environ["STREAMLIT_WATCHDOG_OBSERVER"] = "none" 

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


# ======================
# config.yaml の読み込み
# ======================
# Streamlit CloudではSecrets管理を推奨するため、config.yamlの存在チェックは慎重に行う
CONFIG_PATH = "config.yaml"
if os.path.exists(CONFIG_PATH):
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as file:
            config = yaml.safe_load(file)
    except Exception as e:
        st.error(f"認証設定の読み込みに失敗しました: {e}")
        st.stop()
else:
    # config.yamlが存在しない場合の代替処理（例: Secretsからの読み込み）
    # 今回はconfig.yamlの構造が不明なため、エラーで停止させる
    st.error(f"認証設定ファイル ({CONFIG_PATH}) が見つかりません。")
    st.stop()


# ======================
# 認証の初期化
# ======================
try:
    authenticator = stauth.Authenticate(
        credentials=config["credentials"],
        cookie_name=config["cookie"]["name"],
        key=config["cookie"]["key"],
        cookie_expiry_days=config["cookie"]["expiry_days"],
    )
except Exception as e:
    st.error(f"ログイン画面の初期化に失敗しました: {e}")
    st.stop()


# ======================
# ログインフォームと認証
# ======================
# 修正点: authenticator.loginの戻り値のアンパック処理を修正
name, authentication_status, username = authenticator.login(
    form_name="ログイン", 
    location="main"
)

# 認証状態の分岐
if authentication_status is False:
    st.error("ユーザー名またはパスワードが間違っています。")
    # st.stop() は不要。Streamlitは再実行されるため、メインロジックに入らなければOK。
elif authentication_status is None:
    st.warning("ユーザー名とパスワードを入力してください。")
    # st.stop() は不要。
    
# ログイン成功後の画面
if authentication_status:
    st.success(f"ようこそ、{name}さん！")
    authenticator.logout("ログアウト", "sidebar")

    st.markdown("---")
    st.subheader("📄 保険自動化システム 管理画面")

    # ======================
    # GEMINI 初期化
    # ======================
    # st.secrets.get() は非推奨。st.secrets["KEY"] を推奨。
    GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        st.error("❌ GEMINI_API_KEY が設定されていません。Secretsに追加してください。")
        st.stop()

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.5-flash")

    # ======================
    # PDF抽出関数
    # ======================
    # @st.cache_data を使用して、関数の再実行を防ぎパフォーマンスを向上させる
    @st.cache_data
    def extract_text_from_pdf(pdf_bytes):
        """PDFからテキスト抽出"""
        try:
            # PyPDF2はバージョン3.0.0以降、PdfReader/PdfWriterへの変更があるため、互換性を考慮
            reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
            text = "\n\n".join([p.extract_text() or "" for p in reader.pages])
            return text.strip()
        except Exception as e:
            st.error(f"PDFテキスト抽出エラー: {e}")
            return ""

    @st.cache_data
    def convert_pdf_to_images(pdf_bytes):
        """PDFを画像に変換"""
        # convert_from_bytesは重い処理のため、キャッシュを推奨
        return convert_from_bytes(pdf_bytes)

    # @st.cache_data はAPI呼び出しを含む関数には適さないため、そのままにする
    def extract_info_with_gemini(pdf_bytes, fields):
        """Gemini APIで情報抽出"""
        
        # 処理状況をユーザーに伝えるためのスピナーを追加
        with st.spinner("Geminiによる情報抽出中..."):
            text = extract_text_from_pdf(pdf_bytes)
            example_json = {f: "" for f in fields}

            # プロンプトをより明確にJSON形式を要求するように修正
            prompt = (
                f"以下の保険見積書から {', '.join(fields)} を抽出し、**必ず**指定されたJSON形式で返してください。\n"
                f"不明な項目は空文字にしてください。JSON形式の例: {json.dumps(example_json, ensure_ascii=False)}"
            )

            try:
                contents = []
                if text:
                    # テキスト抽出が成功した場合
                    contents.append({"text": prompt})
                    contents.append({"text": text})
                else:
                    # テキスト抽出が失敗した場合（画像として処理）
                    images = convert_pdf_to_images(pdf_bytes)
                    contents.append({"text": prompt})
                    for img in images:
                        buf = io.BytesIO()
                        # 画像の品質とサイズを考慮してJPEGに変換（PNGよりファイルサイズが小さくなることが多い）
                        img.save(buf, format="JPEG", quality=90) 
                        contents.append(img) # PIL Imageオブジェクトを直接渡す

                # generate_contentの引数を修正: contentsがリストの場合はそのまま渡す
                response = model.generate_content(contents)

                if not response or not response.text:
                    raise ValueError("Geminiの応答が空です。")

                # JSONパースの堅牢性を高めるために、応答からJSONブロックを抽出
                clean_text = response.text.strip()
                if clean_text.startswith("```json"):
                    clean_text = clean_text.strip("```json").strip("```").strip()
                
                return json.loads(clean_text)
            except Exception as e:
                # エラーメッセージに元のファイル名を含める
                raise RuntimeError(f"PDF抽出エラー: {e}")

    # ======================
    # アプリ本体
    # ======================
    st.markdown('<div class="section-header">📁 1. 顧客情報ファイルをアップロード</div>', unsafe_allow_html=True)
    customer_file = st.file_uploader("顧客情報.xlsx をアップロード", type=["xlsx"], key="customer_uploader")
    
    # セッションステートの初期化をファイルのアップロード前に移動
    if "fields" not in st.session_state:
        st.session_state["fields"] = ["氏名", "生年月日", "保険会社名", "保険期間", "保険金額", "補償内容"]

    if customer_file:
        try:
            df_customer = pd.read_excel(customer_file)
            # 列名が変更された場合のみfieldsを更新
            new_fields = df_customer.columns.tolist()
            if st.session_state["fields"] != new_fields:
                st.session_state["fields"] = new_fields
                st.info("💡 アップロードされたExcelファイルの列名が抽出フィールドとして設定されました。")
            
            st.success("✅ 顧客情報ファイルを読み込みました。")
            st.dataframe(df_customer, use_container_width=True)
            st.session_state["customer_df"] = df_customer # 顧客データもセッションに保存
        except Exception as e:
            st.error(f"Excelファイルの読み込みエラー: {e}")
            del st.session_state["fields"] # エラー時は初期値に戻す
            
    # 抽出フィールドの表示
    st.info(f"現在の抽出フィールド: {', '.join(st.session_state['fields'])}")


    st.markdown('<div class="section-header">📄 2. 見積書PDFから情報抽出</div>', unsafe_allow_html=True)
    uploaded_pdfs = st.file_uploader("PDFファイルをアップロード（複数可）", type=["pdf"], accept_multiple_files=True, key="pdf_uploader")
    
    # 抽出結果のセッションステート初期化
    if "comparison_df" not in st.session_state:
        st.session_state["comparison_df"] = pd.DataFrame()

    if uploaded_pdfs and st.button("PDFから情報を抽出", key="extract_button"):
        results = []
        fields = st.session_state["fields"]

        # プログレスバーの追加
        progress_bar = st.progress(0)
        total_pdfs = len(uploaded_pdfs)

        for i, pdf in enumerate(uploaded_pdfs):
            st.info(f"[{i+1}/{total_pdfs}] {pdf.name} を処理中...")
            try:
                # PDFの読み込みはストリームを使用し、読み込み後にポインタをリセット
                pdf_bytes = pdf.read()
                pdf.seek(0) # ストリームをリセット
                
                data = extract_info_with_gemini(pdf_bytes, fields)
                data["ファイル名"] = pdf.name
                results.append(data)
                st.success(f"✅ {pdf.name} 抽出成功")
            except Exception as e:
                st.error(f"❌ {pdf.name} 抽出失敗: {str(e)}")
            
            # プログレスバーを更新
            progress_bar.progress((i + 1) / total_pdfs)
        
        progress_bar.empty() # 完了したらプログレスバーを消す

        if results:
            df = pd.DataFrame(results)
            st.session_state["comparison_df"] = df
            st.dataframe(df, use_container_width=True)
        else:
            st.warning("PDFから情報を抽出できませんでした。")

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
            file_name="見積情報比較表.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    else:
        st.info("まだ抽出結果はありません。")

    st.markdown("---")
    st.markdown("**保険業務自動化アシスタント** | Secure Login + Gemini 2.5 Flash + Streamlit")
