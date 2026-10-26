import io
import os
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
from pydantic import BaseModel, Field
from typing import List, Optional

#class Item(BaseModel):
#    product_name: str = Field(description="商品名")
#    price: int = Field(description="価格（税抜きまたは税込の数字のみ）")
#    capacity: Optional[str] = Field(None, description="容量・規格（例: 100g, 1袋, 1000mlなど。不明な場合はnull）")

#class FlyerExtraction(BaseModel):
#    store_name: str = Field(description="スーパーや店舗の名前")
#    items: List[Item] = Field(description="抽出された商品のリスト")

# 各商品の詳細
class FlyerItem(BaseModel):
    product_name: str = Field(..., description="商品名")
    capacity: Optional[str] = Field(None, description="容量やサイズ（例: 500ml, 1kg）")
    price: int = Field(..., description="価格（税抜または税込の数値）")
    manufacturer: Optional[str] = Field(None, description="メーカー名（判別できる場合。例: 明治、コカ・コーラなど）")
    start_date: Optional[str] = Field(None, description="セールの適用開始日。必ず 'yyyy/mm/dd' 形式（例: 2026/10/01）で抽出してください。年が記載されていない場合は現在の西暦を補完し、判別できない場合は null にしてください。")
    end_date: Optional[str] = Field(None, description="セールの適用終了日。必ず 'yyyy/mm/dd' 形式（例: 2026/10/07）で抽出してください。年が記載されていない場合は現在の西暦を補完し、判別できない場合は null にしてください。")
    recognition_rate: Optional[str] = Field(None, description="判読率 (判読できている確立を最大100%として、正しく判読できている確立を示す）")


# チラシ全体の抽出結果
class FlyerResponse(BaseModel):
    store_name: str = Field(..., description="店舗名（スーパーの名前など）")
    items: List[FlyerItem] = Field(..., description="抽出された商品リスト")

# 更新用データモデル
class FlyerItemUpdate(BaseModel):
    id: str  # レコードの固有ID
    store_name: Optional[str] = None
    product_name: Optional[str] = None
    price: Optional[int] = None
    capacity: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    manufacturer: Optional[str] = None




# ==========================================
# 3. APIエンドポイントの実装
# ==========================================
@app.post("/extract", summary="チラシ画像から商品情報を抽出してDBに保存")
async def extract_and_save_flyer(file: UploadFile = File(...)):
    """
    スマホアプリからアップロードされた画像を受信し、
    Gemini APIで解析してSupabaseへ保存します。
    """

    # 1. 画像ファイルのバリデーション・読み込み（拡張子やMIMEタイプを柔軟にチェック）
    filename = file.filename.lower() if file.filename else ""
    is_image_ext = filename.endswith((".jpg", ".jpeg", ".png", ".heic", ".webp"))
    
    if not (file.content_type.startswith("image/") or is_image_ext):
        raise HTTPException(status_code=400, detail="アップロードされたファイルは画像ではありません。")
    
    try:
        image_bytes = await file.read()
        image = Image.open(io.BytesIO(image_bytes))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"画像の読み込みに失敗しました: {str(e)}")


    # 2. Gemini APIへのプロンプト定義と構造化出力リクエスト
  #  prompt = """
  #  添付されたチラシ画像から、店舗名と記載されているすべての商品情報（商品名、価格、容量）を正確に抽出してください。
  #  価格はカンマや「円」を除いた半角数字のみで出力してください。
  #  """
    prompt = """
    添付されたチラシ画像から、店舗名と記載されているすべての商品情報を正確に抽出してください。

    【抽出項目とルール】
    1. 店舗名 (store_name)
    2. 商品リスト (items):
       - 商品名 (product_name)
       - 容量・サイズ (capacity: 例「500ml」「1kg」など。記載がない場合はnull)
       - 価格 (price: カンマや「円」を除いた半角数字のみ、税込みと税抜きを切り分け、税込み金額を採用する。単位が「%」の場合は価格とみなさない。)
       - メーカー名 (manufacturer: 判別できる場合のみ記載。不明な場合はnull)
       - 適用開始日 (start_date: セールの開始日。必ず "yyyy/mm/dd" 形式（例: 2026/10/01）で抽出してください。年が省略されている場合は現在年を補完し、現在月が12月の時、月が1月の時は次年度の1月とみなす、複数の日付が存在する場合、枠線で囲まれている部分別に日付を判断してください。判断できない場合は日付の一番古い日をセットする)
       - 適用終了日 (end_date: セールの終了日。必ず "yyyy/mm/dd" 形式（例: 2026/10/07）で抽出してください。年が省略されている場合は現在年を補完し、現在月が12月の時、月が1月の時は次年度の1月とみなす、判断できない場合は日付の一番古い日をセットする)
       - 判読率 (recognition_rate: 商品名を正しく判読できている確立を最大100%として、判読できている確立を示す）
    """


    try:
        # 最新のGeminiモデルを呼び出し
        response = genai_client.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=[image, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=FlyerResponse,  # ← FlyerResponse に変更する
                temperature=0.1,
            ),
        )
        
        # Pydanticモデルとしてパース
        extracted_data = FlyerResponse.model_validate_json(response.text)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gemini APIによる解析エラー: {str(e)}")

    # 3. 抽出データを Supabase (flyer_items テーブル) に一括挿入 (INSERT)
    db_records = []
    for item in extracted_data.items:
        db_records.append({
            "store_name": extracted_data.store_name,
            "product_name": item.product_name,
            "capacity": item.capacity,
            "price": item.price,
            "manufacturer": item.manufacturer,
            "start_date": item.start_date,
            "end_date": item.end_date,
            "recognition_rate": item.recognition_rate,
        })
    
    if db_records:
        try:
            # Supabaseへまとめてデータ挿入
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


# ① データ取得用エンドポイント
@app.get("/items")
async def get_all_items():
    try:
        # 登録日時の新しい順に全件取得
        response = supabase.table("flyer_items").select("*").order("created_at", desc=True).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ② 一括更新用エンドポイント（⑥ 修正された行のレコードを更新）
@app.put("/items/batch")
async def update_items_batch(items: List[FlyerItemUpdate]):
    try:
        for item in items:
            update_data = item.dict(exclude_unset=True)
            item_id = update_data.pop("id", None)
            if item_id is not None:
                supabase.table("flyer_items").update(update_data).eq("id", item_id).execute()
        return {"status": "success", "message": "正常に更新しました"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        
# 商品名検索用エンドポイント
@app.get("/items/search")
async def search_items(keyword: str = ""):
    try:
        # ベースのクエリ作成
        query = supabase.table("flyer_items").select("*")
        
        # キーワードが指定されている場合は部分一致検索 (LIKE '%キーワード%')
        if keyword:
            query = query.ilike("product_name", f"%{keyword}%")
            
        # ご指定の並び順（order by capacity, manufacturer, product_name, price）
        response = (
            query.order("capacity")
                 .order("manufacturer")
                 .order("product_name")
                 .order("price")
                 .execute()
        )
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))