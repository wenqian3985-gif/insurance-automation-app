import PyPDF2
from PIL import Image
import io
import os
import json
from pdf2image import convert_from_path
import base64
import google.generativeai as genai

# 環境変数からAPIキーを取得
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise EnvironmentError("GEMINI_API_KEYが設定されていません。")

genai.configure(api_key=api_key)

# モデル初期化（モデル名を統一）
model = genai.GenerativeModel("gemini-1.5-flash")

def convert_pdf_to_images(pdf_path):
    # PDFを画像に変換
    images = convert_from_path(pdf_path)
    return images

def extract_insurance_info_with_gemini_vision(images):
    user_content = [
        {
            "type": "text",
            "text": "以下の保険見積書の内容から、保険会社名、保険期間、保険金額、補償内容を抽出してください。抽出した情報はJSON形式で出力してください。"
        },
        {
            "type": "text",
            "text": "例: {\"保険会社名\": \"架空保険株式会社\", \"保険期間\": \"2025年10月1日～2026年9月30日\", \"保険金額\": \"10,000,000円\", \"補償内容\": \"入院日額5,000円\"}"
        }
    ]

    for image in images:
        byte_arr = io.BytesIO()
        image.save(byte_arr, format='PNG')
        encoded_image = base64.b64encode(byte_arr.getvalue()).decode('utf-8')
        user_content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{encoded_image}"
            }
        })

    # Gemini Vision API呼び出し
    response = model.generate_content(
        user_content,
        generation_config={"response_mime_type": "application/json"}
    )
    return response.text

if __name__ == "__main__":
    pdf_path = "sample_insurance_quote.pdf"
    
    # PDFを画像に変換
    print("--- PDFを画像に変換中 ---")
    images = convert_pdf_to_images(pdf_path)
    print(f"--- {len(images)} ページを画像に変換しました ---")
    
    # Gemini Vision APIを使用して情報を抽出
    insurance_info_json_str = extract_insurance_info_with_gemini_vision(images)
    print("\n--- Geminiによって抽出された保険情報 (JSON) ---")
    print(insurance_info_json_str)

    # JSON文字列をパースする前にMarkdownコードブロックを削除
    if insurance_info_json_str.startswith("```json") and insurance_info_json_str.endswith("```"):
        insurance_info_json_str = insurance_info_json_str[len("```json\n"):-len("\n```")]

    # JSON文字列をパースしてファイルに保存
    try:
        insurance_info_dict = json.loads(insurance_info_json_str)
        with open("extracted_insurance_info.json", "w", encoding="utf-8") as f:
            json.dump(insurance_info_dict, f, ensure_ascii=False, indent=4)
        print("抽出された保険情報が extracted_insurance_info.json に保存されました。")
    except json.JSONDecodeError as e:
        print(f"エラー: Geminiからの応答が有効なJSON形式ではありませんでした。{e}")
        with open("extracted_insurance_info_raw.txt", "w", encoding="utf-8") as f:
            f.write(insurance_info_json_str)
        print("生の応答が extracted_insurance_info_raw.txt に保存されました。")

