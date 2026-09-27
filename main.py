import io
from fastapi import FastAPI, File, UploadFile, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional
from PIL import Image
from google import genai
from google.genai import types
from supabase import create_client, Client

# ==========================================
# 1. 環境変数からの設定読み込み（安全な構成）
# ==========================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Gemini API クライアント
genai_client = genai.Client(api_key=GEMINI_API_KEY)

# Supabase クライアント
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# FastAPI アプリケーションの定義
app = FastAPI(title="チラシ価格データ抽出API")


# ==========================================
# 2. Pydanticデータモデル定義（構造化抽出用）
# ==========================================
class Item(BaseModel):
    product_name: str = Field(description="商品名")
    price: int = Field(description="価格（税抜きまたは税込の数字のみ）")
    capacity: Optional[str] = Field(None, description="容量・規格（例: 100g, 1袋, 1000mlなど。不明な場合はnull）")

class FlyerExtraction(BaseModel):
    store_name: str = Field(description="スーパーや店舗の名前")
    items: List[Item] = Field(description="抽出された商品のリスト")


# ==========================================
# 3. APIエンドポイントの実装
# ==========================================
@app.post("/extract", summary="チラシ画像から商品情報を抽出してDBに保存")
async def extract_and_save_flyer(file: UploadFile = File(...)):
    """
    スマホアプリからアップロードされた画像を受信し、
    Gemini APIで解析してSupabaseへ保存します。
    """
    # 1. 画像ファイルのバリデーション・読み込み
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="アップロードされたファイルは画像ではありません。")
    
    try:
        image_bytes = await file.read()
        image = Image.open(io.BytesIO(image_bytes))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"画像の読み込みに失敗しました: {str(e)}")

    # 2. Gemini APIへのプロンプト定義と構造化出力リクエスト
    prompt = """
    添付されたチラシ画像から、店舗名と記載されているすべての商品情報（商品名、価格、容量）を正確に抽出してください。
    価格はカンマや「円」を除いた半角数字のみで出力してください。
    """

    try:
        # 最新のGeminiモデルを呼び出し
        response = genai_client.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=[image, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=FlyerExtraction,
                temperature=0.1,
            ),
        )
        
        # Pydanticモデルとしてパース
        extracted_data = FlyerExtraction.model_validate_json(response.text)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gemini APIによる解析エラー: {str(e)}")

    # 3. 抽出データを Supabase (flyer_items テーブル) に括挿入 (INSERT)
    db_records = []
    for item in extracted_data.items:
        db_records.append({
            "store_name": extracted_data.store_name,
            "product_name": item.product_name,
            "price": item.price,
            "capacity": item.capacity
        })

    if db_records:
        try:
            # Supabaseへデータ挿入
            supabase.table("flyer_items").insert(db_records).execute()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"データベース保存エラー: {str(e)}")

    # 4. レスポンス（スマホアプリへの返却データ）
    return {
        "status": "success",
        "message": f"{len(db_records)}件の商品データを抽出し、DBに保存しました。",
        "data": extracted_data
    }


# サーバ起動用の動作テスト確認ルート
@app.get("/")
def read_root():
    return {"message": "チラシ抽出APIサーバーは正常に稼働しています。"}