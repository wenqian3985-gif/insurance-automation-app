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
import logging
# GCS関連のライブラリをインポート
from google.cloud import storage
from google.oauth2 import service_account
import sys
import datetime # ログのタイムスタンプ用

# ======================
# JSTタイムゾーン定義 (UTC+9)
# ======================
JST = datetime.timezone(datetime.timedelta(hours=+9), 'JST')

# ======================
# GCSログ設定
# ======================

# st.cache_resourceを使い、GCSクライアントを一度だけ初期化
@st.cache_resource
def init_gcs_client():
    """
    st.secretsからサービスアカウント情報を読み込み、GCSクライアントを初期化する
    """
    try:
        # st.secretsからサービスアカウントの認証情報を直接読み込む
        gcs_credentials_info = st.secrets["gcs_service_account"]
        credentials = service_account.Credentials.from_service_account_info(gcs_credentials_info)
        client = storage.Client(credentials=credentials)
        
        # バケット名もst.secretsから取得
        # secrets.tomlの [gcs_config] セクションで定義されている 'bucket_name' キーを使用します。
        bucket_name = st.secrets["gcs_config"]["bucket_name"] 
        # バケットが存在するか確認 (権限チェック)
        client.get_bucket(bucket_name)
        
        return client
    except KeyError as ke:
        # KeyErrorが発生した場合、どのキーが存在しないかを出力に追加
        st.error(f"❌ GCS認証情報またはバケット名がsecrets.tomlに設定されていません。不足キー: {ke}")
        return None
    except Exception as e:
        st.error(f"❌ GCSクライアントの初期化に失敗しました: {e}")
        return None

# GCSクライアントを初期化
gcs_client = init_gcs_client()

# ======================
# コンソールログ設定 (デバッグ用)
# ======================

# ロガー設定 (コンソール出力用)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

if not logger.hasHandlers(): # ハンドラが未設定の場合のみ追加
    # フォーマッタ設定
    log_format = logging.Formatter(
        fmt='%(asctime)s - %(levelname)s - USER:%(user)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Console Handler: コンソール（ターミナル）に常時出力する
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_format)
    console_handler.setLevel(logging.DEBUG)
    logger.addHandler(console_handler)

def log_user_action(action_description):
    """
    ユーザーのアクションをロギングするヘルパー関数
    1. コンソール (manage app) に出力
    2. GCSバケットのログファイルに追記
    """
    
    # 認証済みユーザー名を取得。未認証の場合は 'UNAUTHENTICATED' を使用
    username = st.session_state.get("username", "UNAUTHENTICATED")
    
    # タイムスタンプ付きのログメッセージを作成
    # ★ 修正: UTC時刻を取得し、JSTに変換してタイムスタンプを作成
    utc_time = datetime.datetime.now(datetime.timezone.utc)
    jst_time = utc_time.astimezone(JST)
    timestamp = jst_time.strftime('%Y-%m-%d %H:%M:%S')
    log_message = f"{timestamp} - INFO - USER:{username} - {action_description}\n" # 末尾に改行を追加

    # --- 1. コンソールへの出力 (即時) ---
    # extra dictを使い、ロガーのフォーマットに 'user' フィールドを渡す
    logger.info(action_description, extra={'user': username})
    for handler in logger.handlers:
        handler.flush()

    # --- 2. GCSへの書き込み (GCSクライアントが正常に初期化されている場合のみ) ---
    if gcs_client:
        try:
            # st.secretsからバケット名とファイル名を取得
            # log_user_actionでは 'bucket_name' を使用しており、init_gcs_clientの修正によりキー名が統一されました。
            bucket_name = st.secrets["gcs_config"]["bucket_name"] 
            log_file_name = st.secrets["gcs_config"]["log_file_name"]
            
            bucket = gcs_client.bucket(bucket_name)
            blob = bucket.blob(log_file_name)

            # GCSの「追記」ロジック:
            # A. 既存のログファイルをダウンロード (存在しない場合は空として扱う)
            if blob.exists():
                existing_log = blob.download_as_string().decode('utf-8')
            else:
                existing_log = ""
                
            # B. 新しいログメッセージを結合
            updated_log = existing_log + log_message
            
            # C. 結合した内容でファイルをアップロード (上書き)
            blob.upload_from_string(updated_log, content_type='text/plain; charset=utf-8')

        except Exception as e:
            # GCSへの書き込み失敗をコンソールに出力 (アプリは停止させない)
            logger.error(f"GCSログファイルへの書き込みに失敗しました: {e}", extra={'user': 'SYSTEM'})

# --- システム起動ログ (ファイルには記録せず、ターミナルのみに出力) ---
logger.debug("システム初期化完了: ロギングシステムをアクティブ化しました。", extra={'user': 'SYSTEM'})
# ------------------------

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
# ネイティブ認証ロジック (最適化済み)
# ======================

# セッション状態の初期化
if "authentication_status" not in st.session_state:
    st.session_state["authentication_status"] = None
if "name" not in st.session_state:
    st.session_state["name"] = None
if "username" not in st.session_state:
    st.session_state["username"] = None
if "extract_messages" not in st.session_state:
    st.session_state["extract_messages"] = []
# ログファイルステータス用のセッションステートは不要になったため削除


def load_and_map_secrets():
    """Secretsからユーザー情報を読み込み、login_usernameをキーとする辞書を生成する"""
    try:
        auth_config = st.secrets["auth_users"]
        mapped_users = {}
        
        # Secretsに定義された全キーから、ユーザー情報を構成するベース名 (例: 'admin') を抽出
        base_users = set(key.rsplit('_', 1)[0]
                             for key in auth_config.keys()
                             if key.endswith(('_username', '_name', '_password')))

        for user_key in base_users:
            username_key = f"{user_key}_username"
            name_key = f"{user_key}_name"
            pass_key = f"{user_key}_password"
            
            # 認証に必要な3つのキーがすべて存在するか確認
            if all(k in auth_config for k in [username_key, name_key, pass_key]):
                
                # 認証辞書のキーには、実際にログイン時に使用する 'username' の値を使用
                login_username = auth_config[username_key]

                mapped_users[login_username] = {
                    "name": auth_config[name_key],
                    "password": auth_config[pass_key]
                }
            
        if not mapped_users:
            st.error("❌ Secretsファイルに有効なユーザー情報が定義されていません。`[auth_users]`セクションを確認してください。")
            st.session_state["authentication_status"] = False
            return {}
        return mapped_users
    except KeyError:
        st.error("❌ Secretsファイルから認証情報 (`auth_users`) を読み込めませんでした。`.streamlit/secrets.toml`の構造を確認してください。")
        st.session_state["authentication_status"] = False
        return {}
    except Exception as e:
        st.error(f" Secretsロード中の予期せぬエラー: {e}")
        st.session_state["authentication_status"] = False
        return {}

# 認証情報辞書のロード (アプリケーション起動時に一度実行)
AUTHENTICATION_USERS = load_and_map_secrets()

def authenticate_user(username, password):
    """ユーザー名と平文パスワードを検証し、セッション状態を更新する"""
    
    if username in AUTHENTICATION_USERS:
        stored_password = AUTHENTICATION_USERS[username]["password"]
        
        if password == stored_password:
            # 認証成功
            st.session_state["authentication_status"] = True
            st.session_state["name"] = AUTHENTICATION_USERS[username]["name"]
            st.session_state["username"] = username
            log_user_action("ログイン成功") # ★ ログ追加: ログイン成功 (GCSに転記)
            return True
    
    # 認証失敗
    st.session_state["authentication_status"] = False
    st.session_state["name"] = None
    st.session_state["username"] = None
    log_user_action(f"ログイン失敗 (試行ユーザー: {username})") # ★ ログ追加: ログイン失敗 (GCSに転記)
    return False

def logout():
    """ログアウト処理"""
    log_user_action("ログアウト") # ★ ログ追加: ログアウト (GCSに転記)
    # 関連するステートを None にリセット
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
        
        st.info("認証が完了するまで、アプリケーションのメイン機能は表示されません。")
        
        # ログ転記ステータスの表示を削除しました
            
else:
    # ログイン成功時のサイドバー表示
    with st.sidebar:
        st.success(f"ようこそ、{st.session_state['name']}さん！")
        if st.button("ログアウト"):
            logout()
            
        # ログ転記ステータスの表示を削除しました

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
            
            # テキストが不十分な場合は画像も追加
            if not text or len(text) < 100:
                st.session_state["extract_messages"].append(f"⚠️ {pdf.name}: テキスト抽出が不十分なため、画像として処理を試みました。")
                try:
                    # PDFを画像に変換して、最初の数ページをContentsに追加
                    images = convert_from_bytes(pdf_bytes)
                    for i, img in enumerate(images[:5]):
                        contents.append(img)
                        if i >= 2: break # 最大3ページまでを画像として送る
                except Exception as img_e:
                    st.session_state["extract_messages"].append(f"❌ {pdf.name}: 画像変換に失敗しました - {img_e}")
            
            # テキストが抽出できた場合はテキストをContentsに追加
            if text and len(text) >= 100:
                contents.append({"text": f"--- PDF TEXT START ---\n{text}"})

            try:
                response = model.generate_content(contents)

                if not response or not response.text:
                    raise ValueError("Geminiの応答が空です。")

                clean_text = response.text.strip()
                if clean_text.startswith("```"):
                    # コードブロック形式で返された場合の処理
                    clean_text = clean_text.replace("```json", "").replace("```", "").strip()
                
                return json.loads(clean_text)
            except json.JSONDecodeError:
                st.session_state["extract_messages"].append(f"❌ {pdf.name}: Gemini応答をJSON解析できませんでした。")
                return None
            except Exception as e:
                st.session_state["extract_messages"].append(f"❌ {pdf.name}: Gemini API呼び出しエラー - {e}")
                return None

    # Gemini APIでデータ分析と提案メッセージ生成
    def analyze_and_generate_proposal(df):
        """データフレームの保険情報を分析し、顧客向け提案メッセージを生成する"""
        
        # DataFrameを文字列形式に変換してプロンプトに含める
        df_string = df.to_string(index=False)
        
        prompt = (
            "以下の保険情報比較表を詳細に分析し、顧客への提案メッセージを作成してください。\n"
            "データは表形式の文字列として提供されます。これを読み取り、適切な形で比較分析を行ってください。\n"
            "データには複数の保険見積書からの抽出情報が含まれている可能性があります。\n"
            "提案メッセージは、以下の要件を満たしてください。\n\n"
            "【提案メッセージ要件】\n"
            "1. 顧客が理解しやすい平易な日本語で記述すること。\n"
            "2. 既存の保険や比較対象の保険の情報を簡潔にまとめ、各項目の違い（特に保険金額、期間、補償内容）を明確に比較すること。\n"
            "3. 分析に基づき、顧客にとって最適な選択肢（または検討すべき点）を専門的な観点から提案すること。\n"
            "4. 提案は親身でプロフェッショナルなトーンで行うこと。\n"
            "5. 回答は提案メッセージ本文のみとし、コードブロックや追加のJSON形式を含めないこと。\n"
            "**6. 提案メッセージの長さは、日本語で最大400文字厳守で簡潔にまとめること。**\n\n"
            "【保険情報比較表データ (列: '氏名', '生年月日', '保険会社名', '保険期間', '保険金額', '補償内容', 'ファイル名' など)】\n"
            f"```data\n{df_string}\n```"
        )

        with st.spinner("🤖 保険情報の比較分析と提案メッセージを生成中..."):
            try:
                # generate_contentを使用し、分析と提案の生成を依頼
                response = model.generate_content(prompt)
                
                if response and response.text:
                    return response.text.strip()
                else:
                    return "Geminiからの提案メッセージを取得できませんでした。"
            except Exception as e:
                return f"提案生成中にエラーが発生しました: {e}"


    # ======================
    # アプリ本体
    # ======================
    
    if "fields" not in st.session_state:
        st.session_state["fields"] = ["氏名", "生年月日", "保険会社名", "保険期間", "保険金額", "補償内容"]
    if "customer_df" not in st.session_state:
        st.session_state["customer_df"] = pd.DataFrame()
    if "comparison_df" not in st.session_state:
        st.session_state["comparison_df"] = pd.DataFrame()
    if "customer_file_name" not in st.session_state: # アップロードファイル名保存用の新しいステート
        st.session_state["customer_file_name"] = None
    if "proposal_message" not in st.session_state: # 提案メッセージ保存用の新しいステートを追加
        st.session_state["proposal_message"] = ""


    st.markdown('<div class="section-header">📁 1. 顧客情報ファイルをアップロード (任意)</div>', unsafe_allow_html=True)
    
    # 1. ファイルアップロードの説明を修正
    customer_file = st.file_uploader("Excelファイルをアップロードした場合は、Excelファイルの項目でPDFの情報を抽出します",
                                     type=["xlsx"], key="customer_uploader")
    
    if customer_file:
        try:
            df_customer = pd.read_excel(customer_file)
            st.session_state["customer_file_name"] = customer_file.name # アップロードファイル名を保存 (要件2)

            new_fields = df_customer.columns.tolist()
            st.session_state["fields"] = new_fields
            st.session_state["customer_df"] = df_customer # 既存データを保存 (要件3)
            
            st.success("✅ 顧客情報ファイルを読み込み、列名を抽出フィールドとして設定しました。")
            log_user_action(f"顧客情報ファイルアップロード: {customer_file.name}") # ★ ログ追加: Excelファイルアップロード (GCSに転記)
            st.dataframe(df_customer, use_container_width=True)

        except Exception as e:
            st.error(f"Excelファイルの読み込みエラー: {e}")
            # エラー時は初期値に戻す
            st.session_state["fields"] = ["氏名", "生年月日", "保険会社名", "保険期間", "保険金額", "補償内容"]
            st.session_state["customer_df"] = pd.DataFrame()
            st.session_state["customer_file_name"] = None
            
    # 2. 抽出フィールドの説明を修正
    default_fields_str = "氏名, 生年月日, 保険会社名, 保険期間, 保険金額, 補償内容"
    if st.session_state["customer_file_name"]:
        # Excelファイルがアップロードされている場合
        field_info = f"現在の抽出フィールド: {', '.join(st.session_state['fields'])}"
    else:
        # Excelファイルがアップロードされていない場合
        field_info = f"Excelファイルをアップロードしない場合は、システム既存項目（{default_fields_str}）でPDF情報を抽出します。"
        
    st.info(field_info)


    st.markdown('<div class="section-header">📄 2. 見積書PDFから情報抽出</div>', unsafe_allow_html=True)
    uploaded_pdfs = st.file_uploader("PDFファイルをアップロード（複数可）", type=["pdf"], accept_multiple_files=True, key="pdf_uploader")
    
    if uploaded_pdfs and st.button("PDFから情報を抽出", key="extract_button"):
        log_user_action(f"PDF抽出開始: {len(uploaded_pdfs)}件のファイル") # ★ ログ追加: PDF抽出開始 (GCSに転記)
        
        # 抽出ボタンが押されたら、以前の提案メッセージと抽出メッセージをクリア
        st.session_state["proposal_message"] = ""
        st.session_state["extract_messages"] = [] # メッセージをリセット
        
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
                    st.session_state["extract_messages"].append(f"✅ {pdf.name} 抽出成功") # メッセージをセッションに保存
                else:
                    st.session_state["extract_messages"].append(f"⚠️ {pdf.name} は抽出に失敗したか、無効な結果を返しました。") # メッセージをセッションに保存
                    
            except Exception as e:
                st.session_state["extract_messages"].append(f"❌ {pdf.name} 処理中に予期せぬエラー: {str(e)}") # メッセージをセッションに保存
            
            progress_bar.progress((i + 1) / total_pdfs)
        
        progress_bar.empty()

        if results:
            df_extracted = pd.DataFrame(results) # PDFから抽出した新しいデータ
            
            # 既存の顧客データがあるかチェック (要件3: 既存データに追記)
            if not st.session_state["customer_df"].empty:
                df_customer = st.session_state["customer_df"].copy()
                
                # 既存データと抽出結果の列を揃えるための列リストを作成
                cols_to_use = df_customer.columns.tolist()
                
                # 要件1: 既存データに「ファイル名」列がない場合、結合のために追加する
                if "ファイル名" not in cols_to_use:
                    cols_to_use.append("ファイル名")
                    
                # 既存データと抽出データの列を cols_to_use に揃える（足りない列はNaNで埋まる）
                df_customer = df_customer.reindex(columns=cols_to_use)
                df_extracted = df_extracted.reindex(columns=cols_to_use)
                
                # 既存データの下に抽出データを追記
                df_final = pd.concat([df_customer, df_extracted], ignore_index=True)
                
            else:
                # 既存データがない場合は、抽出結果のみを使用
                fields = st.session_state["fields"]
                # 順序を設定: 抽出フィールド + ファイル名 (要件1: ファイル名がfieldsにない場合は最後に追加)
                column_order = [f for f in fields if f in df_extracted.columns]
                if "ファイル名" in df_extracted.columns and "ファイル名" not in column_order:
                     column_order.append("ファイル名")

                df_final = df_extracted.reindex(columns=column_order)
            
            # FIX: Streamlit/PyArrowのValueError (混合データ型) を避けるため、
            # DataFrameを表示・保存する前に全ての列を文字列に変換する
            df_final = df_final.astype(str)
                
            st.session_state["comparison_df"] = df_final
            log_user_action(f"PDF抽出完了: {len(results)}件のレコードを比較表に追加") # ★ ログ追加: PDF抽出完了 (GCSに転記)
        else:
            if not st.session_state["extract_messages"]:
                st.session_state["extract_messages"].append("PDFから情報を抽出できませんでした。処理ログを確認してください。")

    # 抽出メッセージの表示
    if st.session_state["extract_messages"]:
        with st.container():
            for msg in st.session_state["extract_messages"]:
                if msg.startswith("✅"):
                    st.success(msg)
                elif msg.startswith("⚠️"):
                    st.warning(msg)
                elif msg.startswith("❌"):
                    st.error(msg)
                else:
                    st.info(msg) # その他のメッセージ

    # 抽出データフレームの表示
    if not st.session_state["comparison_df"].empty:
        st.dataframe(st.session_state["comparison_df"], use_container_width=True)

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
        
        # ダウンロードファイル名設定 (ユーザーの要求: アップロードファイル名と同一、またはデフォルト)
        download_filename = "見積情報比較表_抽出結果.xlsx"
        if st.session_state.get("customer_file_name"):
            # アップロードファイル名をそのまま使用
            download_filename = st.session_state["customer_file_name"]
            
        st.download_button(
            "📥 Excelでダウンロード",
            data=excel_data,
            file_name=download_filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            on_click=lambda: log_user_action(f"抽出結果ダウンロード: {download_filename}") # ★ ログ追加: ダウンロード (GCSに転記)
        )
    else:
        st.info("まだ抽出結果はありません。")

    # 新しいセクションを追加
    st.markdown('<div class="section-header">💬 4. 比較分析と提案メッセージの作成</div>', unsafe_allow_html=True)
    if not st.session_state["comparison_df"].empty:
        
        if st.button("提案メッセージを作成・表示", key="analyze_button"):
            log_user_action("提案メッセージ生成開始") # ★ ログ追加: 提案生成開始 (GCSに転記)
            # 提案メッセージを生成し、セッションに保存
            proposal = analyze_and_generate_proposal(st.session_state["comparison_df"])
            st.session_state["proposal_message"] = proposal
            log_user_action("提案メッセージ生成完了") # ★ ログ追加: 提案生成完了 (GCSに転記)
            
        if st.session_state["proposal_message"]:
            st.markdown("---")
            st.markdown("### 顧客向け提案メッセージ")
            # 提案メッセージをMarkdownとして表示
            st.markdown(st.session_state["proposal_message"])
            st.markdown("---")
        elif "proposal_message" in st.session_state:
            st.info("提案メッセージを作成するには、上のボタンを押してください。")
            
    else:
        st.info("比較分析を行うには、先にPDFから情報を抽出してください。")


    st.markdown("---")
    st.markdown("**保険業務自動化アシスタント** | Streamlit + Gemini 2.5 Flash")