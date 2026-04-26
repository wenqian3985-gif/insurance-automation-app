import os
import io
import sys
import re
import json
import time
import logging
import datetime
import tempfile
from typing import List, Dict, Any

import streamlit as st
import pandas as pd
import pymupdf4llm
import google.generativeai as genai
from pdf2image import convert_from_bytes
from PIL import Image

from google.cloud import storage
from google.oauth2 import service_account

# 各社専用プロンプトのインポート
from extractors.tokio import build_tokio_step1_prompt, build_tokio_step2_prompt
from extractors.sompo import build_sompo_step1_prompt, build_sompo_step2_prompt
from extractors.mitsui import build_mitsui_step1_prompt, build_mitsui_step2_prompt

# ======================
# JSTタイムゾーン定義 (UTC+9)
# ======================
JST = datetime.timezone(datetime.timedelta(hours=+9), "JST")

# ======================
# デフォルト抽出項目（ヒアリング後の24項目）
# ======================
DEFAULT_FIELDS = [
    "保険会社",
    "プラン",
    "氏名",
    "建築年月",
    "広さ",
    "建物構造",
    "物件種別",
    "建物_基本_保険金額",
    "建物_地震_保険金額",
    "家財_基本_保険金額",
    "家財_地震_保険金額",
    "保険期間",
    "保険料",
    "所在地",
    "火災、落雷、破裂・爆発",
    "風災、雹(ひょう)災、雪災",
    "水濡れ",
    "盗難",
    "水災",
    "破損、汚損等",
    "地震・噴火・津波",
    "盗難・水濡（ぬ）れ等",
    "物体の落下、飛来、水濡れ、騒じょう",
    "不測かつ突発的な事故",
    "抽出日",
    "ファイル名"
]

# ======================
# GCSログ設定
# ======================
@st.cache_resource
def init_gcs_client():
    try:
        gcs_credentials_info = dict(st.secrets["gcs_service_account"])
        credentials = service_account.Credentials.from_service_account_info(gcs_credentials_info)
        client = storage.Client(credentials=credentials)
        bucket_name = st.secrets["gcs_config"]["bucket_name"]
        client.get_bucket(bucket_name)
        return client
    except KeyError as ke:
        st.error(f"❌ GCS認証情報またはバケット名がsecrets.tomlに設定されていません。不足キー: {ke}")
        return None
    except Exception as e:
        st.error(f"❌ GCSクライアントの初期化に失敗しました: {e}")
        return None

gcs_client = init_gcs_client()

# ======================
# コンソールログ設定
# ======================
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

if not logger.hasHandlers():
    log_format = logging.Formatter(
        fmt="%(asctime)s - %(levelname)s - USER:%(user)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_format)
    console_handler.setLevel(logging.DEBUG)
    logger.addHandler(console_handler)

def log_user_action(action_description: str):
    username = st.session_state.get("username", "UNAUTHENTICATED")
    utc_time = datetime.datetime.now(datetime.timezone.utc)
    jst_time = utc_time.astimezone(JST)
    timestamp = jst_time.strftime("%Y-%m-%d %H:%M:%S")
    log_message = f"{timestamp} - INFO - USER:{username} - {action_description}\n"

    logger.info(action_description, extra={"user": username})
    for handler in logger.handlers:
        handler.flush()

    if gcs_client:
        try:
            bucket_name = st.secrets["gcs_config"]["bucket_name"]
            log_file_name = st.secrets["gcs_config"]["log_file_name"]
            bucket = gcs_client.bucket(bucket_name)
            blob = bucket.blob(log_file_name)

            if blob.exists():
                existing_log = blob.download_as_string().decode("utf-8")
            else:
                existing_log = ""

            updated_log = existing_log + log_message
            blob.upload_from_string(updated_log, content_type="text/plain; charset=utf-8")
        except Exception as e:
            logger.error(f"GCSログファイルへの書き込みに失敗しました: {e}", extra={"user": "SYSTEM"})

logger.debug("システム初期化完了: ロギングシステムをアクティブ化しました。", extra={"user": "SYSTEM"})

# ======================
# 環境設定・デザイン
# ======================
st.set_page_config(page_title="保険業務自動化アシスタント", layout="wide")

st.markdown(
    """
<style>
html, body, [class*="css"] {
    font-family: "Noto Sans JP", "Meiryo", "Yu Gothic", sans-serif;
}
.main-header {
    font-size: 2.2rem;
    font-weight: 800;
    color: #1f77b4;
    text-align: center;
    margin-bottom: 1.5rem;
}
.section-header {
    font-size: 1.4rem;
    font-weight: bold;
    color: #2ca02c;
    margin-top: 1.5rem;
    margin-bottom: 0.8rem;
    border-bottom: 2px solid #ddd;
    padding-bottom: 5px;
}
.stButton>button {
    border-radius: 8px;
    border: 1px solid #2ca02c;
    color: white;
    background-color: #2ca02c;
}
</style>
""",
    unsafe_allow_html=True,
)

st.markdown('<div class="main-header">🏥 保険業務自動化アシスタント</div>', unsafe_allow_html=True)

# ======================
# ネイティブ認証ロジック
# ======================
if "authentication_status" not in st.session_state:
    st.session_state["authentication_status"] = None
if "name" not in st.session_state:
    st.session_state["name"] = None
if "username" not in st.session_state:
    st.session_state["username"] = None
if "extract_messages" not in st.session_state:
    st.session_state["extract_messages"] = []
if "fields" not in st.session_state:
    st.session_state["fields"] = DEFAULT_FIELDS.copy()
if "customer_df" not in st.session_state:
    st.session_state["customer_df"] = pd.DataFrame()
if "comparison_df" not in st.session_state:
    st.session_state["comparison_df"] = pd.DataFrame()
if "customer_file_name" not in st.session_state:
    st.session_state["customer_file_name"] = None
if "proposal_message" not in st.session_state:
    st.session_state["proposal_message"] = ""

def load_and_map_secrets():
    try:
        auth_config = st.secrets["auth_users"]
        mapped_users = {}

        base_users = {
            key.rsplit("_", 1)[0]
            for key in auth_config.keys()
            if key.endswith(("_username", "_name", "_password"))
        }

        for user_key in base_users:
            username_key = f"{user_key}_username"
            name_key = f"{user_key}_name"
            pass_key = f"{user_key}_password"

            if all(k in auth_config for k in [username_key, name_key, pass_key]):
                login_username = str(auth_config[username_key])
                mapped_users[login_username] = {
                    "name": str(auth_config[name_key]),
                    "password": str(auth_config[pass_key]),
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

AUTHENTICATION_USERS = load_and_map_secrets()

def authenticate_user(username: str, password: str):
    if username in AUTHENTICATION_USERS:
        stored_password = AUTHENTICATION_USERS[username]["password"]
        if password == stored_password:
            st.session_state["authentication_status"] = True
            st.session_state["name"] = AUTHENTICATION_USERS[username]["name"]
            st.session_state["username"] = username
            log_user_action("ログイン成功")
            return True
    st.session_state["authentication_status"] = False
    st.session_state["name"] = None
    st.session_state["username"] = None
    log_user_action(f"ログイン失敗 (試行ユーザー: {username})")
    return False

def logout():
    log_user_action("ログアウト")
    st.session_state["authentication_status"] = None
    st.session_state["name"] = None
    st.session_state["username"] = None
    st.info("ログアウトしました。")
    time.sleep(1)
    st.rerun()

# ======================
# 項目名ゆれ吸収用辞書（24項目向け簡易版）
# ======================
FIELD_ALIASES = {
    "保険会社": ["保険会社", "保険会社名", "引受保険会社", "会社名"],
    "プラン": ["プラン", "ご案内プラン", "ご契約プラン", "プラン識別子", "コース"],
    "氏名": ["法人名", "氏名", "ご氏名", "契約者名", "被保険者名", "記名被保険者", "様"],
    "保険期間": ["期間", "保険期間", "始期日・保険期間", "契約期間"],
    "保険料": ["保険料", "合計保険料", "総払込保険料", "1回分保険料", "基本保険料", "年間保険料"],
    "建築年月": ["築年月", "建築年月", "築年月　"],
    "広さ": ["広さ", "広さ　", "面積", "延床面積", "専有延面積", "専（占）有面積"],
    "建物構造": ["建物構造", "構造", "建物形態", "構造級別"],
    "所在地": ["所在地", "保険の対象の所在地", "物件の所在地"],
    "物件種別": ["物件種別", "用法", "建物形態", "専用住宅", "共同住宅"],
}

def normalize_label(label: str) -> str:
    if label is None: return ""
    return str(label).replace("\u3000", "").replace(" ", "").strip()

def clean_value(v: Any) -> str:
    if v is None: return ""
    s = str(v).strip()
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s

def get_aliases_for_field(field_name: str) -> List[str]:
    raw = FIELD_ALIASES.get(field_name, [])
    if raw: return raw
    normalized_target = normalize_label(field_name)
    for k, v in FIELD_ALIASES.items():
        if normalize_label(k) == normalized_target:
            return v
    return [field_name]

def japanese_ratio(text: str) -> float:
    if not text: return 0.0
    jp_chars = re.findall(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff10-\uff19\uff21-\uff3a\uff41-\uff5a]", text)
    return len(jp_chars) / max(len(text), 1)

def detect_insurer(pdf_name: str, text: str) -> str:
    joined = f"{pdf_name}\n{text}"
    if "損保ジャパン" in joined or "ＴＨＥ すまいの保険" in joined:
        return "損保ジャパン"
    if "東京海上" in joined or "トータルアシスト 住まいの保険" in joined or ("住まいの保険" in joined and "東京海上" in pdf_name):
        return "東京海上日動"
    if "三井住友海上" in joined or "ＧＫ すまいの保険" in joined or "GK すまいの保険" in joined:
        return "三井住友海上"
    return ""

def expected_plan_count(insurer: str) -> int:
    if insurer in ["三井住友海上", "損保ジャパン", "東京海上日動"]:
        return 3
    return 1

def normalize_extracted_record(record: Dict[str, Any], fields: List[str], pdf_name: str, insurer: str) -> Dict[str, Any]:
    output_fields = list(dict.fromkeys(["保険会社", "プラン"] + fields + ["プラン識別子"]))
    normalized = {f: "" for f in output_fields}

    for k, v in record.items():
        if k in normalized:
            normalized[k] = clean_value(v)

    for field in output_fields:
        if normalized[field]:
            continue
        norm_field = normalize_label(field)
        for src_key, src_val in record.items():
            if normalize_label(src_key) == norm_field:
                normalized[field] = clean_value(src_val)
                break

    for field in output_fields:
        if normalized[field]:
            continue
        aliases = get_aliases_for_field(field)
        alias_norms = [normalize_label(a) for a in aliases]
        for src_key, src_val in record.items():
            if normalize_label(src_key) in alias_norms:
                normalized[field] = clean_value(src_val)
                break

    normalized["保険会社"] = normalized.get("保険会社") or clean_value(insurer)
    normalized["プラン"] = normalized.get("プラン") or clean_value(record.get("プラン識別子", ""))
    normalized["プラン識別子"] = clean_value(record.get("プラン識別子", normalized.get("プラン", "")))
    if not normalized["プラン"] and normalized["プラン識別子"]:
        normalized["プラン"] = normalized["プラン識別子"]

    normalized["ファイル名"] = pdf_name
    return normalized

# ======================
# PDF解析・データ抽出基盤
# ======================
@st.cache_data
def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """PDFをMarkdown形式に高精度変換"""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
        temp_pdf.write(pdf_bytes)
        temp_pdf_path = temp_pdf.name
    try:
        md_text = pymupdf4llm.to_markdown(temp_pdf_path)
        return md_text
    except Exception as e:
        print(f"Markdown変換エラー: {e}")
        return ""
    finally:
        if os.path.exists(temp_pdf_path):
            os.remove(temp_pdf_path)

@st.cache_data
def convert_pdf_to_images(pdf_bytes: bytes):
    return convert_from_bytes(pdf_bytes, dpi=220)

def pil_image_to_gemini_part(img: "Image.Image") -> Dict[str, Any]:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    import base64
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return {
        "inline_data": {
            "mime_type": "image/jpeg",
            "data": b64,
        }
    }

def build_multi_plan_prompt(fields: List[str], pdf_name: str, insurer: str, retry_mode: bool = False) -> str:
    """保険会社不明の場合の汎用プロンプト"""
    all_keys = list(dict.fromkeys(fields + ["保険会社", "プラン", "プラン識別子"]))
    numbered_keys = "\n".join([f"{i+1}. {k}" for i, k in enumerate(all_keys)])
    retry_extra = "\n・前回プラン数不足。必ず全プランを別オブジェクトで返すこと。" if retry_mode else ""

    return (
        "あなたは日本の火災保険見積書から情報を抽出するアシスタントです。\n"
        "以下のルールに従い、JSON配列のみを返してください。\n\n"
        "【絶対ルール】\n"
        "・返却形式: JSON配列のみ。マークダウン符号の使用禁止\n"
        "・1プラン = 1JSONオブジェクト\n"
        "・保険会社はPDFファイル名またはPDF本文から判定して出力\n"
        "・プランはPDF上の見出しをそのまま出力（例：プラン①、プラン１、Ⅲコース）\n"
        "・プラン識別子はプランと同じ値を出力\n"
        "・共通情報（氏名・所在地・広さ等）は全プランに同じ値を複製\n"
        "・補償の有無は『〇』または『』（空欄）で出力"
        + retry_extra + "\n\n"
        "【キー一覧】\n"
        + numbered_keys
    )

def extract_json_from_text(text: str) -> Any:
    # 表示エラーを回避するためバッククォート3つを `{3}` として正規表現処理
    text = re.sub(r"^`{3}(?:json)?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"`{3}$", "", text).strip()
    try: return json.loads(text)
    except: pass
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try: return json.loads(text[start:end + 1])
        except: pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try: return json.loads(text[start:end + 1])
        except: pass
    return None

def call_gemini_for_plan_rows(model, contents: List[Any], fields: List[str], pdf_name: str, insurer: str):
    try:
        response = model.generate_content(
            contents,
            generation_config={"temperature": 0},
        )
        if not response or not response.text:
            raise ValueError("Geminiの応答が空です。")

        if "debug_raw_responses" not in st.session_state:
            st.session_state["debug_raw_responses"] = []
        st.session_state["debug_raw_responses"].append({
            "file": pdf_name,
            "raw": response.text[:3000]
        })

        parsed = extract_json_from_text(response.text)
        if parsed is None:
            st.session_state["extract_messages"].append(f"❌ {pdf_name}: Gemini応答をJSON解析できませんでした。")
            return []

        if isinstance(parsed, dict): parsed = [parsed]
        if not isinstance(parsed, list): raise ValueError("Gemini応答がJSON配列ではありません。")

        normalized_rows = []
        for item in parsed:
            if not isinstance(item, dict): continue
            row = normalize_extracted_record(item, fields, pdf_name, insurer)
            normalized_rows.append(row)

        return normalized_rows

    except Exception as e:
        st.session_state["extract_messages"].append(f"❌ {pdf_name}: Gemini API呼び出しエラー - {e}")
        return []

def extract_info_with_gemini_multi_plan(pdf_bytes: bytes, fields: List[str], pdf_name: str, model):
    with st.spinner(f"[{pdf_name}] Markdown変換と2段階抽出を実行中..."):
        text = extract_text_from_pdf(pdf_bytes)
        text_quality = japanese_ratio(text)
        insurer = detect_insurer(pdf_name, text)
        expected_count = expected_plan_count(insurer)

        st.session_state["extract_messages"].append(
            f"ℹ️ {pdf_name}: 保険会社={insurer or '不明'}, "
            f"テキスト品質={text_quality:.2f}, 期待プラン数={expected_count}"
        )

        images = []
        try:
            pil_images = convert_pdf_to_images(pdf_bytes)
            images = [pil_image_to_gemini_part(img) for img in pil_images[:4]]
        except Exception as img_e:
            st.session_state["extract_messages"].append(f"⚠️ {pdf_name}: 画像変換失敗 - {img_e}")

        # --- 2段階抽出: ステップ1（基本情報の抽出） ---
        common_info = {}
        if insurer in ["東京海上日動", "損保ジャパン", "三井住友海上"]:
            if insurer == "東京海上日動":
                prompt_step1 = build_tokio_step1_prompt()
            elif insurer == "損保ジャパン":
                prompt_step1 = build_sompo_step1_prompt()
            elif insurer == "三井住友海上":
                prompt_step1 = build_mitsui_step1_prompt()

            contents_step1 = [{"text": f"【Markdown Data】\n{text}\n\n{prompt_step1}"}]
            contents_step1.extend(images)
            try:
                response_step1 = model.generate_content(contents_step1)
                common_info = extract_json_from_text(response_step1.text)
                if isinstance(common_info, list) and len(common_info) > 0:
                    common_info = common_info[0]
                if not isinstance(common_info, dict):
                    common_info = {}
            except Exception as e:
                print(f"ステップ1エラー: {e}")
                common_info = {}

            # --- 2段階抽出: ステップ2（プラン詳細の抽出） ---
            if insurer == "東京海上日動":
                prompt_1 = build_tokio_step2_prompt(fields, common_info)
            elif insurer == "損保ジャパン":
                prompt_1 = build_sompo_step2_prompt(fields, common_info)
            elif insurer == "三井住友海上":
                prompt_1 = build_mitsui_step2_prompt(fields, common_info)
        else:
            prompt_1 = build_multi_plan_prompt(fields, pdf_name, insurer, retry_mode=False)

        contents_1 = [{"text": f"【Markdown Data】\n{text}\n\n{prompt_1}"}]
        contents_1.extend(images)

        rows_1 = call_gemini_for_plan_rows(model, contents_1, fields, pdf_name, insurer)

        # プラン数不足の場合のリトライ
        if len(rows_1) < expected_count:
            if insurer in ["東京海上日動", "損保ジャパン", "三井住友海上"]:
                if insurer == "東京海上日動":
                    prompt_retry = build_tokio_step2_prompt(fields, common_info)
                elif insurer == "損保ジャパン":
                    prompt_retry = build_sompo_step2_prompt(fields, common_info)
                elif insurer == "三井住友海上":
                    prompt_retry = build_mitsui_step2_prompt(fields, common_info)
                prompt_retry += "\n\n【重要】プラン数が不足しています。必ず全プラン出力してください。"
            else:
                prompt_retry = build_multi_plan_prompt(fields, pdf_name, insurer, retry_mode=True)
                
            contents_2 = [{"text": f"【Markdown Data】\n{text}\n\n{prompt_retry}"}] + images
            rows_2 = call_gemini_for_plan_rows(model, contents_2, fields, pdf_name, insurer)
            if len(rows_2) > len(rows_1):
                rows_1 = rows_2

        # 抽出日・ファイル名の付与と重複排除
        today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        unique_rows = []
        seen = set()
        for row in rows_1:
            row["保険会社"] = clean_value(row.get("保険会社", "")) or insurer
            row["プラン"] = clean_value(row.get("プラン", "")) or clean_value(row.get("プラン識別子", ""))
            row["プラン識別子"] = clean_value(row.get("プラン識別子", "")) or row["プラン"]
            row["抽出日"] = today
            row["ファイル名"] = pdf_name
            key = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                seen.add(key)
                unique_rows.append(row)

        return unique_rows

# ======================
# AIによる提案メッセージ生成
# ======================
def analyze_and_generate_proposal(df: pd.DataFrame, model) -> str:
    df_string = df.to_string(index=False)
    prompt = (
        "以下の保険情報比較表を詳細に分析し、顧客への提案メッセージを作成してください。\n"
        "【要件】\n"
        "1. 平易な日本語で記述。\n"
        "2. 各プランの違い（保険料、期間、補償内容）を比較。\n"
        "3. 最適な選択肢を提案。\n"
        "4. 親身でプロフェッショナルなトーン。\n"
        "5. 提案メッセージ本文のみ（マークダウン等不要）。\n"
        "6. 400文字以内で簡潔に。\n\n"
        f"【データ】\n{df_string}"
    )
    with st.spinner("🤖 保険情報の比較分析と提案メッセージを生成中..."):
        try:
            response = model.generate_content(prompt)
            if response and response.text:
                return response.text.strip()
            return "Geminiからの提案メッセージを取得できませんでした。"
        except Exception as e:
            return f"提案生成中にエラーが発生しました: {e}"

@st.cache_data
def to_excel_bytes(df: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="見積情報比較表")
    return output.getvalue()


# ======================
# メインUIロジック
# ======================
if st.session_state["authentication_status"] is not True:
    with st.sidebar:
        st.title("ログイン")
        username_input = st.text_input("ユーザー名")
        password_input = st.text_input("パスワード", type="password")

        if st.button("ログイン"):
            if authenticate_user(username_input, password_input):
                st.success("ログイン成功！")
                st.rerun()
            else:
                st.error("ユーザー名またはパスワードが間違っています。")
        st.info("認証が完了するまで、アプリケーションのメイン機能は表示されません。")
else:
    with st.sidebar:
        st.success(f"ようこそ、{st.session_state['name']}さん！")
        if st.button("ログアウト"):
            logout()

if st.session_state["authentication_status"]:
    st.markdown("---")
    st.subheader("📄 保険自動化システム メイン機能")

    try:
        GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
        if not GEMINI_API_KEY:
            st.error("❌ Secretsファイルに `GEMINI_API_KEY` が設定されていません。")
            st.stop()
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.5-flash")
    except KeyError:
        st.error("❌ SecretsファイルからAPIキーを読み込めませんでした。")
        st.stop()
    except Exception as e:
        st.error(f"❌ Gemini初期化エラー: {e}")
        st.stop()

    st.markdown('<div class="section-header">📁 1. 顧客情報ファイルをアップロード (任意)</div>', unsafe_allow_html=True)
    col1, col2 = st.columns([3, 1])

    with col1:
        customer_file = st.file_uploader(
            "Excelファイルをアップロードした場合は、Excelファイルの項目でPDFの情報を抽出します",
            type=["xlsx"],
            key="customer_uploader",
        )

    with col2:
        if st.button("既定項目に戻す", key="reset_default_fields_button"):
            st.session_state["fields"] = DEFAULT_FIELDS.copy()
            st.session_state["customer_df"] = pd.DataFrame()
            st.session_state["customer_file_name"] = None
            log_user_action("抽出項目を既定項目にリセット")
            st.rerun()

    if customer_file:
        try:
            df_customer = pd.read_excel(customer_file)
            df_customer.columns = [str(c) for c in df_customer.columns]
            st.session_state["customer_file_name"] = customer_file.name
            st.session_state["fields"] = df_customer.columns.tolist()
            st.session_state["customer_df"] = df_customer

            st.success("✅ 顧客情報ファイルを読み込み、列名を抽出フィールドとして設定しました。")
            log_user_action(f"顧客情報ファイルアップロード: {customer_file.name}")
            st.dataframe(df_customer, use_container_width=True)
        except Exception as e:
            st.error(f"Excelファイルの読み込みエラー: {e}")
            st.session_state["fields"] = DEFAULT_FIELDS.copy()
            st.session_state["customer_df"] = pd.DataFrame()
            st.session_state["customer_file_name"] = None

    field_count = len(st.session_state["fields"])
    if st.session_state["customer_file_name"]:
        summary_text = f"現在の抽出フィールド: {st.session_state['customer_file_name']} から設定（計 {field_count} 項目）"
    else:
        summary_text = f"現在の抽出フィールド（システム既定）: 計 {field_count} 項目"

    st.info(summary_text)
    with st.expander("🔽 抽出項目の詳細リストを確認する"):
        st.write(", ".join(st.session_state["fields"]))

    st.markdown('<div class="section-header">📄 2. 見積書PDFから情報抽出</div>', unsafe_allow_html=True)
    uploaded_pdfs = st.file_uploader(
        "PDFファイルをアップロード（複数可）",
        type=["pdf"],
        accept_multiple_files=True,
        key="pdf_uploader",
    )

    if uploaded_pdfs and st.button("PDFから情報を抽出", key="extract_button"):
        log_user_action(f"PDF抽出開始: {len(uploaded_pdfs)}件のファイル")
        st.session_state["proposal_message"] = ""
        st.session_state["extract_messages"] = []
        st.session_state["debug_raw_responses"] = []

        results = []
        fields = st.session_state["fields"]
        progress_bar = st.progress(0)
        total_pdfs = len(uploaded_pdfs)

        for i, pdf in enumerate(uploaded_pdfs):
            try:
                pdf_bytes = pdf.read()
                rows = extract_info_with_gemini_multi_plan(pdf_bytes, fields, pdf.name, model)
                if rows:
                    results.extend(rows)
                    st.session_state["extract_messages"].append(f"✅ {pdf.name} 抽出成功（{len(rows)}プラン）")
                else:
                    st.session_state["extract_messages"].append(f"⚠️ {pdf.name} は抽出に失敗したか、プランを認識できませんでした。")
            except Exception as e:
                st.session_state["extract_messages"].append(f"❌ {pdf.name} 処理中に予期せぬエラー: {str(e)}")
            progress_bar.progress((i + 1) / total_pdfs)

        progress_bar.empty()

        if results:
            df_extracted = pd.DataFrame(results)
            extra_cols = ["保険会社", "プラン", "プラン識別子", "ファイル名", "抽出日"]

            if not st.session_state["customer_df"].empty:
                df_customer = st.session_state["customer_df"].copy()
                cols_to_use = df_customer.columns.tolist()
                for c in extra_cols:
                    if c not in cols_to_use:
                        cols_to_use.append(c)
                df_customer = df_customer.reindex(columns=cols_to_use)
                df_extracted = df_extracted.reindex(columns=cols_to_use)
                df_final = pd.concat([df_customer, df_extracted], ignore_index=True)
            else:
                base_cols = st.session_state["fields"].copy()
                for c in extra_cols:
                    if c not in base_cols:
                        base_cols.append(c)
                df_final = df_extracted.reindex(columns=base_cols)

            df_final = df_final.fillna("").astype(str)
            st.session_state["comparison_df"] = df_final
            log_user_action(f"PDF抽出完了: {len(results)}件のレコードを比較表に追加")
        else:
            if not st.session_state["extract_messages"]:
                st.session_state["extract_messages"].append("PDFから情報を抽出できませんでした。処理ログを確認してください。")

    if st.session_state["extract_messages"]:
        with st.container():
            for msg in st.session_state["extract_messages"]:
                if msg.startswith("✅"): st.success(msg)
                elif msg.startswith("⚠️"): st.warning(msg)
                elif msg.startswith("❌"): st.error(msg)
                else: st.info(msg)

    if not st.session_state["comparison_df"].empty:
        st.dataframe(st.session_state["comparison_df"], use_container_width=True)

    if st.session_state.get("debug_raw_responses"):
        with st.expander("🔍 Gemini生レスポンス（デバッグ用）", expanded=False):
            for entry in st.session_state["debug_raw_responses"]:
                st.markdown(f"**{entry['file']}**")
                st.code(entry["raw"], language="json")

    st.markdown('<div class="section-header">📊 3. 抽出結果をダウンロード</div>', unsafe_allow_html=True)
    if not st.session_state["comparison_df"].empty:
        excel_data = to_excel_bytes(st.session_state["comparison_df"])
        download_filename = "見積情報比較表_抽出結果.xlsx"
        if st.session_state.get("customer_file_name"):
            download_filename = st.session_state["customer_file_name"]

        if st.download_button(
            "📥 Excelでダウンロード",
            data=excel_data,
            file_name=download_filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ):
            log_user_action(f"抽出結果ダウンロード: {download_filename}")
    else:
        st.info("まだ抽出結果はありません。")

    st.markdown('<div class="section-header">💬 4. 比較分析と提案メッセージの作成</div>', unsafe_allow_html=True)
    if not st.session_state["comparison_df"].empty:
        if st.button("提案メッセージを作成・表示", key="analyze_button"):
            log_user_action("提案メッセージ生成開始")
            proposal = analyze_and_generate_proposal(st.session_state["comparison_df"], model)
            st.session_state["proposal_message"] = proposal
            log_user_action("提案メッセージ生成完了")

        if st.session_state["proposal_message"]:
            st.markdown("---")
            st.markdown("### 顧客向け提案メッセージ")
            st.markdown(st.session_state["proposal_message"])
            st.markdown("---")
        else:
            st.info("提案メッセージを作成するには、上のボタンを押してください。")
    else:
        st.info("比較分析を行うには、先にPDFから情報を抽出してください。")

    st.markdown("---")
    st.markdown("**保険業務自動化アシスタント** | Streamlit + Gemini 2.5 Flash")