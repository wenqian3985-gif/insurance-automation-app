import os
import io
import sys
import re
import json
import time
import logging
import datetime
from typing import List, Dict, Any

import streamlit as st
import pandas as pd
import PyPDF2
import google.generativeai as genai
from pdf2image import convert_from_bytes
from PIL import Image

from google.cloud import storage
from google.oauth2 import service_account


# ======================
# JSTタイムゾーン定義 (UTC+9)
# ======================
JST = datetime.timezone(datetime.timedelta(hours=+9), "JST")


# ======================
# デフォルト抽出項目
# 添付「各種証明書発行依頼書_20240611.xlsx」の列名を反映
# ======================
DEFAULT_FIELDS = [
    "法人名",
    "築年月　",
    "広さ　",
    "建物構造",
    "補償内容",
    "保険金額",
    "期間",
    "保険料",
    "用法",
    "所在地",
    "構造級別",
    "専有延面積",
    "建築年月",
    "所有関係",
    "火災",
    "落雷",
    "破裂",
    "爆発",
    "風災",
    "雹災",
    "雪災",
    "水災",
    "盗難",
    "物体の落下",
    "飛来",
    "水濡れ",
    "騒じょう",
    "不測かつ突発的な事故",
    "地震",
    "地震火災費用",
    "凍結水道管修理費用",
    "臨時費用",
    "個人賠償責任",
    "自己負担額",
    "弁護士費用",
    "類焼損害",
    "住宅修理トラブル",
    "合計保険料",
    "基本情報",
    "評価情報",
    "建物に含まれる物",
    "基礎",
    "門",
    "塀",
    "垣",
    "物置",
    "車庫",
    "職作業",
    "共同住宅戸室数",
    "標準的な評価額",
    "噴火",
    "津波",
    "事故時諸費用特約",
    "地震火災費用特約",
    "防犯対策費用特約",
    "特別費用保険金特約",
    "日常生活賠償",
    "受託物賠償",
    "失火見舞費用",
    "建物",
    "家財",
    "什器",
    "設備",
    "免責金額",
    "始期日",
    "満期日",
    "払込方法",
    "物件種別",
    "約定割合",
]


# ======================
# GCSログ設定
# ======================
@st.cache_resource
def init_gcs_client():
    """
    st.secretsからサービスアカウント情報を読み込み、GCSクライアントを初期化する
    """
    try:
        gcs_credentials_info = st.secrets["gcs_service_account"]
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

        base_users = set(
            key.rsplit("_", 1)[0]
            for key in auth_config.keys()
            if key.endswith(("_username", "_name", "_password"))
        )

        for user_key in base_users:
            username_key = f"{user_key}_username"
            name_key = f"{user_key}_name"
            pass_key = f"{user_key}_password"

            if all(k in auth_config for k in [username_key, name_key, pass_key]):
                login_username = auth_config[username_key]
                mapped_users[login_username] = {
                    "name": auth_config[name_key],
                    "password": auth_config[pass_key],
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
# 項目名ゆれ吸収用辞書
# ※ キーは必ず DEFAULT_FIELDS（またはカスタム fields）内の列名と一致させること
# ======================
FIELD_ALIASES = {
    "法人名": ["法人名", "氏名", "ご氏名", "契約者名", "被保険者名", "記名被保険者", "様"],
    "氏名": ["氏名", "ご氏名", "契約者名", "被保険者名", "記名被保険者", "様"],
    "生年月日": ["生年月日"],
    "保険会社名": ["保険会社名", "引受保険会社", "保険会社"],
    # ----------------------------------------
    # 【修正①】"期間" のエイリアスに "保険期間" を明示追加
    # ----------------------------------------
    "期間": ["期間", "保険期間", "始期日・保険期間", "契約期間", "保険期間（年）"],
    "保険期間": ["保険期間", "期間", "始期日・保険期間", "契約期間"],
    # ----------------------------------------
    # 【修正②】"保険金額" のエイリアスを追加
    # ----------------------------------------
    "保険金額": ["保険金額", "支払限度額", "支払限度額（保険金額）", "保険金額（建物）", "保険金額（家財）", "保険金額合計"],
    # ----------------------------------------
    # 【修正③】"補償内容" のエイリアスを追加
    # ----------------------------------------
    "補償内容": ["補償内容", "プラン名", "ご契約プラン", "ご案内プラン", "コース", "プラン", "ご案内コース"],
    "保険料": ["保険料", "合計保険料", "総払込保険料", "1回分保険料", "基本保険料", "年間保険料"],
    "合計保険料": ["合計保険料", "保険料", "総払込保険料", "1回分保険料"],
    "築年月": ["築年月", "建築年月"],
    "築年月　": ["築年月", "建築年月"],
    "建築年月": ["建築年月", "築年月"],
    "広さ": ["広さ", "面積", "延床面積", "専有延面積", "専（占）有面積"],
    "広さ　": ["広さ", "面積", "延床面積", "専有延面積", "専（占）有面積"],
    "専有延面積": ["専有延面積", "延床面積", "面積", "専（占）有面積"],
    "建物構造": ["建物構造", "構造", "建物形態", "構造級別"],
    "構造級別": ["構造級別", "構造"],
    "所在地": ["所在地", "保険の対象の所在地", "物件の所在地"],
    "用法": ["用法", "＜用法＞", "物件種別", "住居区分", "専用住宅"],
    "物件種別": ["物件種別", "用法", "建物形態"],
    "所有関係": ["所有関係", "建物の所有関係", "所有"],
    "職作業": ["職作業", "職作業名"],
    "共同住宅戸室数": ["共同住宅戸室数", "居住用戸室数"],
    "標準的な評価額": ["標準的な評価額", "評価額"],
    "始期日": ["始期日", "開始日"],
    "満期日": ["満期日"],
    "払込方法": ["払込方法", "払込方法・払込回数"],
    "約定割合": ["約定割合"],
    "建物": ["建物", "住まいの保険 建物", "保険の対象 建物"],
    "家財": ["家財", "保険の対象 家財"],
    "什器": ["什器"],
    "設備": ["設備"],
    "免責金額": ["免責金額", "自己負担額"],
    "自己負担額": ["自己負担額", "免責金額"],
    "基本情報": ["基本情報"],
    "評価情報": ["評価情報"],
    "建物に含まれる物": ["建物に含まれる物"],
    "基礎": ["基礎"],
    "門": ["門"],
    "塀": ["塀"],
    "垣": ["垣"],
    "物置": ["物置"],
    "車庫": ["車庫"],
    "火災": ["火災", "火災、落雷、破裂・爆発"],
    "落雷": ["落雷", "火災、落雷、破裂・爆発"],
    "破裂": ["破裂", "火災、落雷、破裂・爆発"],
    "爆発": ["爆発", "火災、落雷、破裂・爆発"],
    "風災": ["風災", "風災、雹災、雪災", "風災、ひょう災、雪災"],
    "雹災": ["雹災", "ひょう災", "風災、雹災、雪災", "風災、ひょう災、雪災"],
    "雪災": ["雪災", "風災、雹災、雪災", "風災、ひょう災、雪災"],
    "水災": ["水災", "水災（水災等地：1）"],
    "盗難": ["盗難"],
    "物体の落下": ["物体の落下", "物体の落下・飛来、水濡れ、騒じょう"],
    "飛来": ["飛来", "物体の落下・飛来、水濡れ、騒じょう"],
    "水濡れ": ["水濡れ", "水ぬれ", "盗難・水濡（ぬ）れ等", "物体の落下・飛来、水濡れ、騒じょう"],
    "騒じょう": ["騒じょう", "物体の落下・飛来、水濡れ、騒じょう"],
    "不測かつ突発的な事故": ["不測かつ突発的な事故", "破損等"],
    "地震": ["地震", "地震保険", "地震・噴火・津波"],
    "地震火災費用": ["地震火災費用", "地震火災費用保険金"],
    "凍結水道管修理費用": ["凍結水道管修理費用", "水道管凍結修理費用保険金"],
    "臨時費用": ["臨時費用", "臨時費用補償"],
    "個人賠償責任": ["個人賠償責任", "個人賠償", "日常生活賠償"],
    "弁護士費用": ["弁護士費用", "弁護士費用日常"],
    "類焼損害": ["類焼損害", "類焼損害補償"],
    "住宅修理トラブル": ["住宅修理トラブル", "住宅修理トラブル弁護士費用"],
    "噴火": ["噴火", "地震・噴火・津波"],
    "津波": ["津波", "地震・噴火・津波"],
    "事故時諸費用特約": ["事故時諸費用特約"],
    "地震火災費用特約": ["地震火災費用特約"],
    "防犯対策費用特約": ["防犯対策費用特約"],
    "特別費用保険金特約": ["特別費用保険金特約"],
    "日常生活賠償": ["日常生活賠償", "個人賠償責任"],
    "受託物賠償": ["受託物賠償"],
    "失火見舞費用": ["失火見舞費用", "類焼１億・見舞費用"],
}


def normalize_label(label: str) -> str:
    if label is None:
        return ""
    return str(label).replace("\u3000", "").replace(" ", "").strip()


def clean_value(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s


def get_aliases_for_field(field_name: str) -> List[str]:
    raw = FIELD_ALIASES.get(field_name, [])
    if raw:
        return raw

    normalized_target = normalize_label(field_name)
    for k, v in FIELD_ALIASES.items():
        if normalize_label(k) == normalized_target:
            return v

    return [field_name]


def japanese_ratio(text: str) -> float:
    if not text:
        return 0.0
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

    if "三井住友海上" in pdf_name:
        return "三井住友海上"
    if "損保ジャパン" in pdf_name:
        return "損保ジャパン"
    if "東京海上" in pdf_name:
        return "東京海上日動"

    return ""


def expected_plan_count(insurer: str) -> int:
    if insurer in ["三井住友海上", "損保ジャパン", "東京海上日動"]:
        return 3
    return 1


def normalize_period_string(value: str) -> str:
    s = clean_value(value)
    s = s.replace("　", " ")
    s = re.sub(r"\s+", " ", s)
    return s


def fill_common_defaults(row: Dict[str, Any], insurer: str):
    if "保険会社名" in row and not clean_value(row.get("保険会社名", "")) and insurer:
        row["保険会社名"] = insurer

    if "補償内容" in row and not clean_value(row.get("補償内容", "")):
        plan_id = clean_value(row.get("プラン識別子", ""))
        if plan_id:
            row["補償内容"] = plan_id

    if "期間" in row:
        row["期間"] = normalize_period_string(row.get("期間", ""))
    if "保険期間" in row:
        row["保険期間"] = normalize_period_string(row.get("保険期間", ""))

    return row


def normalize_extracted_record(record: Dict[str, Any], fields: List[str], pdf_name: str, insurer: str) -> Dict[str, Any]:
    # fields の列名のみで初期化（Geminiが返した余分なキーは混入させない）
    normalized = {f: "" for f in fields}

    # ステップ1: キーが完全一致する場合はそのまま代入
    for k, v in record.items():
        if k in normalized:
            normalized[k] = clean_value(v)

    # ステップ2: normalize_label で全角スペース・半角スペースを除去してマッチング
    for field in fields:
        if normalized[field]:
            continue
        norm_field = normalize_label(field)
        for src_key, src_val in record.items():
            if normalize_label(src_key) == norm_field:
                normalized[field] = clean_value(src_val)
                break

    # ステップ3: FIELD_ALIASES によるエイリアスマッチング
    for field in fields:
        if normalized[field]:
            continue
        aliases = get_aliases_for_field(field)
        alias_norms = [normalize_label(a) for a in aliases]
        for src_key, src_val in record.items():
            if normalize_label(src_key) in alias_norms:
                normalized[field] = clean_value(src_val)
                break

    # 保険料・合計保険料のフォールバック
    if "保険料" in normalized and not normalized["保険料"]:
        for k in ["合計保険料", "総払込保険料", "1回分保険料"]:
            if clean_value(record.get(k, "")):
                normalized["保険料"] = clean_value(record.get(k, ""))
                break

    if "合計保険料" in normalized and not normalized["合計保険料"]:
        for k in ["保険料", "総払込保険料", "1回分保険料"]:
            if clean_value(record.get(k, "")):
                normalized["合計保険料"] = clean_value(record.get(k, ""))
                break

    # ファイル名・プラン識別子は常に付与（fieldsに含まれていなくても管理用に追加）
    normalized["ファイル名"] = pdf_name
    normalized["プラン識別子"] = clean_value(
        record.get("プラン識別子")
        or record.get("プラン名")
        or record.get("ご契約プラン")
        or record.get("ご案内プラン")
        or record.get("コース")
        or ""
    )

    normalized = fill_common_defaults(normalized, insurer)
    return normalized


@st.cache_data
def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        text = "\n\n".join([p.extract_text() or "" for p in reader.pages])
        return text.strip()
    except Exception as e:
        print(f"PDFテキスト抽出エラー（PyPDF2）: {e}")
        return ""


@st.cache_data
def convert_pdf_to_images(pdf_bytes: bytes):
    return convert_from_bytes(pdf_bytes, dpi=220)





def build_multi_plan_prompt(fields: List[str], pdf_name: str, insurer: str, retry_mode: bool = False) -> str:
    insurer_hint = ""
    if insurer == "三井住友海上":
        insurer_hint = "\n・Ⅰコース / Ⅱコース / Ⅲコース を別プランとして分けること"
    elif insurer == "損保ジャパン":
        insurer_hint = "\n・プラン1 / プラン2 / プラン3 を別プランとして分けること"
    elif insurer == "東京海上日動":
        insurer_hint = "\n・プラン① / プラン② / プラン③ を別プランとして分けること"

    # 番号付きキー一覧（Geminiが全キーを確実に認識できるよう番号付きリスト形式）
    all_keys = fields + ["プラン識別子", "ファイル名"]
    numbered_keys = "\n".join([f"{i+1}. {k}" for i, k in enumerate(all_keys)])

    # エイリアス対応表（意味のあるものだけ）
    alias_lines = []
    for f in fields:
        aliases = get_aliases_for_field(f)
        meaningful = [a for a in aliases if normalize_label(a) != normalize_label(f)]
        if meaningful:
            alias_txt = "・".join(meaningful[:3])
            alias_lines.append(f'  "{f}" <- PDF上では "{alias_txt}" とも表記')
    alias_section = "\n".join(alias_lines)

    retry_extra = "\n・前回プラン数不足。必ず全プランを別オブジェクトで返すこと。" if retry_mode else ""

    prompt = (
        "あなたは日本の火災保険見積書から情報を抽出するアシスタントです。\n"
        "以下のルールに従い、JSON配列のみを返してください。\n\n"
        "【絶対ルール】\n"
        "・返却形式: JSON配列のみ。説明文・マークダウン・コードブロック一切不要\n"
        "・1プラン = 1JSONオブジェクト（複数プランは複数オブジェクト）\n"
        "・全オブジェクトは必ず下記【キー一覧】の全キーを持つこと（PDFにない項目は空文字 \"\" ）\n"
        "・JSONキーは【キー一覧】の文字列をそのまま使うこと（独自キー・勝手な変換禁止）\n"
        "・PDFに書かれた内容のみ抽出（推測・補完禁止）\n"
        "・共通情報（契約者名・所在地・面積・建築年月等）は全プランに同じ値を複製\n"
        "・補償項目（火災・水災等）の値は ○ / × / 金額 をそのまま入れてよい\n"
        "・「自己負担額」と「免責金額」は同義として扱ってよい\n"
        "・「法人名」には契約者名・氏名・被保険者名を入れてよい\n"
        "・「補償内容」にはプラン名・コース名を入れてよい\n"
        "・「期間」には保険始期〜年数をまとめて入れてよい"
        + insurer_hint + retry_extra + "\n\n"
        "【キー一覧】（必ず全キーをJSONオブジェクトに含めること）\n"
        + numbered_keys + "\n\n"
        "【PDF上の表現との対応】\n"
        + alias_section + "\n\n"
        '【出力例（形式のみ、値は実際のPDFから）】\n'
        '[{"プラン識別子":"プランA","法人名":"山田太郎","期間":"2025/4/1〜1年",'
        '"保険料":"30,000円","火災":"○","水災":"×",'
        '"築年月　":"2010年3月","広さ　":"85㎡",...（全キーを含む）}]\n'
    )
    return prompt


def extract_json_from_text(text: str) -> Any:
    """
    Geminiの応答テキストからJSONを抽出する。
    コードブロック・前後の説明文・不完全なJSONに対応するフォールバック付き。
    """
    # Step1: コードブロック除去
    text = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()

    # Step2: そのままパース試行
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Step3: 最初の [ ... ] を探してパース
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    # Step4: 最初の { ... } を探してオブジェクトとしてパース
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    # Step5: 不完全なJSON配列の末尾修復を試みる（最後のカンマ・不完全オブジェクトを除去）
    start = text.find("[")
    if start != -1:
        candidate = text[start:]
        # 末尾の不完全なオブジェクトを削除してみる
        last_complete = candidate.rfind("},")
        if last_complete != -1:
            candidate = candidate[:last_complete + 1] + "]"
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

    return None


def call_gemini_for_plan_rows(model, contents: List[Any], fields: List[str], pdf_name: str, insurer: str):
    try:
        # response_mime_type は画像入力と組み合わせると不安定なためテキストモードで返させる
        response = model.generate_content(
            contents,
            generation_config={
                "temperature": 0,
            },
        )

        if not response or not response.text:
            raise ValueError("Geminiの応答が空です。")

        # デバッグ用: 生レスポンスを保存
        if "debug_raw_responses" not in st.session_state:
            st.session_state["debug_raw_responses"] = []
        st.session_state["debug_raw_responses"].append({
            "file": pdf_name,
            "raw": response.text[:3000]
        })

        parsed = extract_json_from_text(response.text)

        if parsed is None:
            st.session_state["extract_messages"].append(
                f"❌ {pdf_name}: Gemini応答をJSON解析できませんでした。\n応答の先頭300文字: {response.text[:300]}"
            )
            return []

        if isinstance(parsed, dict):
            parsed = [parsed]

        if not isinstance(parsed, list):
            raise ValueError("Gemini応答がJSON配列ではありません。")

        normalized_rows = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            row = normalize_extracted_record(item, fields, pdf_name, insurer)
            normalized_rows.append(row)

        return normalized_rows

    except Exception as e:
        st.session_state["extract_messages"].append(f"❌ {pdf_name}: Gemini API呼び出しエラー - {e}")
        return []


def pil_image_to_gemini_part(img: "Image.Image") -> Dict[str, Any]:
    """PIL ImageをGemini APIが受け付けるinline_data形式に変換する"""
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


def extract_info_with_gemini_multi_plan(pdf_bytes: bytes, fields: List[str], pdf_name: str, model):
    with st.spinner(f"[{pdf_name}] Geminiによる複数プラン抽出中..."):
        # テキスト抽出（日本語PDFは文字化けする場合あり）
        text = extract_text_from_pdf(pdf_bytes)
        text_quality = japanese_ratio(text)

        # 保険会社検出: テキストが取れない場合もファイル名から検出
        insurer = detect_insurer(pdf_name, text)
        expected_count = expected_plan_count(insurer)

        st.session_state["extract_messages"].append(
            f"ℹ️ {pdf_name}: 保険会社={insurer or '不明'}, "
            f"テキスト品質={text_quality:.2f}, 期待プラン数={expected_count}"
        )

        # PDF→画像変換
        images = []
        try:
            pil_images = convert_pdf_to_images(pdf_bytes)
            images = [pil_image_to_gemini_part(img) for img in pil_images[:6]]
            st.session_state["extract_messages"].append(
                f"ℹ️ {pdf_name}: 画像変換成功 ({len(images)}ページ)"
            )
        except Exception as img_e:
            st.session_state["extract_messages"].append(
                f"⚠️ {pdf_name}: 画像変換失敗 - {img_e}"
            )

        # コンテンツ構築: プロンプト + テキスト（取れた場合）+ 画像
        prompt_1 = build_multi_plan_prompt(fields, pdf_name, insurer, retry_mode=False)
        contents_1 = [{"text": prompt_1}]

        if text and text_quality >= 0.12:
            contents_1.append({"text": f"--- PDF TEXT START ---\n{text}\n--- PDF TEXT END ---"})

        contents_1.extend(images)

        rows_1 = call_gemini_for_plan_rows(model, contents_1, fields, pdf_name, insurer)

        # プラン数不足の場合はリトライ（画像のみで再試行）
        if len(rows_1) < expected_count:
            prompt_2 = build_multi_plan_prompt(fields, pdf_name, insurer, retry_mode=True)
            contents_2 = [{"text": prompt_2}] + images
            rows_2 = call_gemini_for_plan_rows(model, contents_2, fields, pdf_name, insurer)
            if len(rows_2) > len(rows_1):
                rows_1 = rows_2

        # 重複排除
        unique_rows = []
        seen = set()
        for row in rows_1:
            key = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                seen.add(key)
                unique_rows.append(row)

        return unique_rows


def analyze_and_generate_proposal(df: pd.DataFrame, model) -> str:
    df_string = df.to_string(index=False)

    prompt = (
        "以下の保険情報比較表を詳細に分析し、顧客への提案メッセージを作成してください。\n"
        "データは表形式の文字列として提供されます。これを読み取り、適切な形で比較分析を行ってください。\n"
        "データには複数の保険見積書からの抽出情報が含まれている可能性があります。\n"
        "提案メッセージは、以下の要件を満たしてください。\n\n"
        "【提案メッセージ要件】\n"
        "1. 顧客が理解しやすい平易な日本語で記述すること。\n"
        "2. 各プランの違い（特に保険料、期間、補償内容）を明確に比較すること。\n"
        "3. 分析に基づき、顧客にとって最適な選択肢（または検討すべき点）を専門的な観点から提案すること。\n"
        "4. 提案は親身でプロフェッショナルなトーンで行うこと。\n"
        "5. 回答は提案メッセージ本文のみとし、コードブロックや追加のJSON形式を含めないこと。\n"
        "6. 提案メッセージの長さは、日本語で最大400文字厳守で簡潔にまとめること。\n\n"
        "【保険情報比較表データ】\n"
        f"```data\n{df_string}\n```"
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
        st.error("❌ SecretsファイルからAPIキーを読み込めませんでした。`GEMINI_API_KEY`キーを確認してください。")
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

    # ※ customer_file が None の場合は session_state["fields"] を変更しない
    #   （session_state 初期化時に DEFAULT_FIELDS がセット済みのため）

    if st.session_state["customer_file_name"]:
        field_info = f"現在の抽出フィールド: {', '.join(st.session_state['fields'])}"
    else:
        field_info = f"現在の抽出フィールド（既定）: {', '.join(DEFAULT_FIELDS)}"

    st.info(field_info)

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
            extra_cols = ["プラン識別子", "ファイル名"]

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
                if msg.startswith("✅"):
                    st.success(msg)
                elif msg.startswith("⚠️"):
                    st.warning(msg)
                elif msg.startswith("❌"):
                    st.error(msg)
                else:
                    st.info(msg)

    if not st.session_state["comparison_df"].empty:
        st.dataframe(st.session_state["comparison_df"], use_container_width=True)

    # デバッグ: Gemini生レスポンス確認用
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