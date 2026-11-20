import traceback
import time
import io
import os
from fastapi import FastAPI, File, UploadFile, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional
from PIL import Image
from google import genai
from google.genai import types
from google.genai.errors import ServerError, APIError
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

# 503エラー時に即座に別モデルへ切り替えるための優先順位リスト
MODELS_TO_TRY = [
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-3.7-flash",
    "gemini-2.5-flash",
]

# 1モデルあたりの無駄なリトライ待機を防ぐ設定
FAST_FAIL_OPTIONS = types.HttpOptions(
    retry_options=types.HttpRetryOptions(attempts=1)
)


# ==========================================
# 2. Pydanticデータモデル定義（構造化抽出用）
# ==========================================

# 各商品の詳細
class FlyerItem(BaseModel):
    product_name: str = Field(..., description="商品名")
    capacity: Optional[str] = Field(None, description="容量やサイズ（例: 500ml, 1kg）")
    price: int = Field(..., description="価格（税抜または税込の数値）")
    manufacturer: Optional[str] = Field(None, description="メーカー名（判別できる場合。例: 明治、コカ・コーラなど）")
    start_date: Optional[str] = Field(None, description="セールの適用開始日。必ず 'yyyy/mm/dd' 形式（例: 2026/10/01）で抽出してください。")
    end_date: Optional[str] = Field(None, description="セールの適用終了日。必ず 'yyyy/mm/dd' 形式（例: 2026/10/07）で抽出してください。")
    recognition_rate: Optional[str] = Field(None, description="判読率 (判読できている確率を最大100%として％で示す)")
    
    # チラシ上の位置情報と並び順
    vertical_pos: Optional[str] = Field(None, description="チラシ全体における縦位置（'上段', '中段', '下段' のいずれか）")
    horizontal_pos: Optional[str] = Field(None, description="チラシ全体における横位置（'左', '中央', '右' のいずれか）")
    bounding_box: Optional[List[int]] = Field(None, description="画像内の商品位置を示すバウンディングボックス [ymin, xmin, ymax, xmax]（0〜1000スケール）")
    sort_order: int = Field(1, description="チラシ上で左上から右、下の行へ進む読取順の番号（1から開始）")


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
    vertical_pos: Optional[str] = None
    horizontal_pos: Optional[str] = None
    sort_order: Optional[int] = None
    bounding_box: Optional[List[int]] = None


# ==========================================
# 3. 座標ベースで「左上→右→下の行→右下」の順を組み立てる関数
# ==========================================
def sort_items_top_left_to_bottom_right(items: List[FlyerItem], row_threshold: int = 40) -> List[FlyerItem]:
    """
    各商品の bounding_box [ymin, xmin, ymax, xmax] (0〜1000) をもとに、
    紙面の左上から右へ向かい、行が変わったら次の行の左端から右へ進み、
    最後が右下になる順番を組み立てて sort_order (1始まり) を付与します。
    """
    def get_coords(item: FlyerItem):
        bbox = item.bounding_box
        if bbox and len(bbox) >= 2:
            ymin = bbox[0] if bbox[0] is not None else 9999
            xmin = bbox[1] if bbox[1] is not None else 9999
            return ymin, xmin
        return 9999, 9999

    # まず上端Y座標（ymin）で仮ソート
    sorted_by_y = sorted(items, key=lambda it: (get_coords(it)[0], get_coords(it)[1]))

    # ymin の近さ（row_threshold以内）で「同じ行グループ」にまとめる
    rows: List[List[FlyerItem]] = []
    current_row: List[FlyerItem] = []
    current_row_ymin: Optional[int] = None

    for item in sorted_by_y:
        ymin, _ = get_coords(item)
        if current_row_ymin is None:
            current_row = [item]
            current_row_ymin = ymin
        elif abs(ymin - current_row_ymin) <= row_threshold:
            current_row.append(item)
        else:
            rows.append(current_row)
            current_row = [item]
            current_row_ymin = ymin

    if current_row:
        rows.append(current_row)

    # 各行の中では左から右（xmin昇順、同じならymin昇順）に並べ、全体を1本のリストへ結合
    ordered_items: List[FlyerItem] = []
    for row in rows:
        row_sorted = sorted(row, key=lambda it: (get_coords(it)[1], get_coords(it)[0]))
        ordered_items.extend(row_sorted)

    # 最終的な並び順に 1 から始まる sort_order を設定
    for idx, item in enumerate(ordered_items, start=1):
        item.sort_order = idx

    return ordered_items


# ==========================================
# 4. APIエンドポイントの実装
# ==========================================
@app.post("/extract", summary="チラシ画像から商品情報を抽出してDBに保存")
async def extract_and_save_flyer(file: UploadFile = File(...)):
    """
    スマホアプリからアップロードされた画像を受信し、
    Gemini APIで解析して座標順（左上→右下）に組み立てたうえでSupabaseへ保存します。
    """

    # 1. 画像ファイルのバリデーション・読み込み
    filename = file.filename.lower() if file.filename else ""
    is_image_ext = filename.endswith((".jpg", ".jpeg", ".png", ".heic", ".webp"))
    content_type = file.content_type or ""
    
    if not (content_type.startswith("image/") or is_image_ext):
        raise HTTPException(status_code=400, detail="アップロードされたファイルは画像ではありません。")
    
    try:
        image_bytes = await file.read()
        image = Image.open(io.BytesIO(image_bytes))
        # API負荷軽減・タイムアウト防止のため長辺を最大1024pxにリサイズ
        image.thumbnail((1024, 1024))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"画像の読み込みに失敗しました: {str(e)}")

    # 2. Gemini APIへのプロンプト定義（位置情報・並び順の指定を追加）
    prompt = """
    添付されたチラシ画像から、店舗名と記載されているすべての商品情報を正確に抽出してください。

    【抽出項目とルール】
    1. 店舗名 (store_name)
    2. 商品リスト (items):
       - 商品名 (product_name)
       - 容量・サイズ (capacity: 例「500ml」「1kg」など。記載がない場合はnull)
       - 価格 (price: カンマや「円」を除いた半角数字のみ、税込みと税抜きを切り分け、税込み金額を採用する。)
       - メーカー名 (manufacturer: 判別できる場合のみ。不明な場合はnull)
       - 適用開始日 (start_date: "yyyy/mm/dd" 形式。不明な場合はnull)
       - 適用終了日 (end_date: "yyyy/mm/dd" 形式。不明な場合はnull)
       - 判読率 (recognition_rate: 正しく判読できている確率)
       - 縦位置 (vertical_pos: チラシ全体を縦に3分割したときの位置。「上段」「中段」「下段」のいずれか)
       - 横位置 (horizontal_pos: チラシ全体を横に3分割したときの位置。「左」「中央」「右」のいずれか)
       - 座標情報 (bounding_box: 画像内の商品位置 [ymin, xmin, ymax, xmax] (0〜1000の相対座標) を必ず出力してください)
       - 読取順 (sort_order: チラシの左上から右へ進み、行が変わったら次の行の左端から右へ進み、最後が右下になる順番で1から連番を振ってください)
    """

    # 3. 複数モデルによるフォールバック解析
    response = None
    last_error = None

    for model_name in MODELS_TO_TRY:
        try:
            print(f"モデル試行中: {model_name}")
            response = genai_client.models.generate_content(
                model=model_name,
                contents=[image, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=FlyerResponse,
                    temperature=0.1,
                    http_options=FAST_FAIL_OPTIONS,
                ),
            )
            print(f"解析成功モデル: {model_name}")
            break

        except (ServerError, APIError) as e:
            print(f"{model_name} 失敗 (status={getattr(e, 'code', 'unknown')}): 次のモデルへ切り替えます -> {e}")
            last_error = e
            time.sleep(0.5)
            continue

        except Exception as e:
            print("=== 予期せぬエラー詳細 ===")
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=f"API Error: {str(e)}")

    if response is None:
        print("=== 全モデル失敗 ===")
        raise HTTPException(
            status_code=503,
            detail=f"現在すべてのAIモデルが混雑しています。少し時間を置いて再度お試しください。({str(last_error)})"
        )

    try:
        # Pydanticモデルとしてパース
        extracted_data = FlyerResponse.model_validate_json(response.text)
    except Exception as e:
        print("=== JSONパースエラー詳細 ===")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"解析結果の変換に失敗しました: {str(e)}")

    # 座標（bounding_box）をもとに「左上 → 右 → 下の行 → 右下」の順番を確定し、sort_order を 1..N で採番
    ordered_items = sort_items_top_left_to_bottom_right(extracted_data.items, row_threshold=40)
    extracted_data.items = ordered_items

    # 4. 並び順確定後のデータを Supabase (flyer_items テーブル) に一括挿入 (INSERT)
    db_records = []
    for item in ordered_items:
        db_records.append({
            "store_name": extracted_data.store_name,
            "product_name": item.product_name,
            "capacity": item.capacity,
            "price": item.price,
            "manufacturer": item.manufacturer,
            "start_date": item.start_date,
            "end_date": item.end_date,
            "recognition_rate": item.recognition_rate,
            "vertical_pos": item.vertical_pos,
            "horizontal_pos": item.horizontal_pos,
            "bounding_box": item.bounding_box,
            "sort_order": item.sort_order,
        })
    
    if db_records:
        try:
            # Supabaseへまとめてデータ挿入
            supabase.table("flyer_items").insert(db_records).execute()
        except Exception as e:
            print("=== Supabase保存エラー詳細 ===")
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=f"データベース保存エラー: {str(e)}")

    # 5. レスポンス（左上→右下の並び順になったデータを返却）
    return {
        "status": "success",
        "message": f"{len(db_records)}件の商品データを左上→右下の順に並べてDBに保存しました。",
        "data": extracted_data
    }


# サーバ起動用の動作テスト確認ルート
@app.get("/")
def read_root():
    return {"message": "チラシ抽出APIサーバーは正常に稼働しています。"}


# ① データ取得用エンドポイント（並び順：sort_order 昇順を優先）
@app.get("/items")
async def get_all_items():
    try:
        # 左上から右・下へ進む sort_order 順で取得
        response = supabase.table("flyer_items").select("*").order("sort_order", desc=False).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ② 一括更新用エンドポイント
@app.put("/items/batch")
async def update_items_batch(items: List[FlyerItemUpdate]):
    try:
        for item in items:
            update_data = item.model_dump(exclude_unset=True)
            item_id = update_data.pop("id", None)
            if item_id is not None:
                supabase.table("flyer_items").update(update_data).eq("id", item_id).execute()
        return {"status": "success", "message": "正常に更新しました"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ③ 商品名検索用エンドポイント
@app.get("/items/search")
async def search_items(keyword: str = ""):
    try:
        query = supabase.table("flyer_items").select("*")
        
        if keyword:
            query = query.ilike("product_name", f"%{keyword}%")
            
        # 検索時もレイアウト順（sort_order）を綺麗に反映させる
        response = query.order("sort_order", desc=False).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))