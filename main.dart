import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';
import 'package:http/http.dart' as http;
import 'dart:convert';

void main() {
  runApp(const MyApp());
}

class MyApp extends StatelessWidget {
  const MyApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'チラシ価格抽出アプリ',
      theme: ThemeData(primarySwatch: Colors.blue),
      home: const FlyerUploadPage(),
    );
  }
}

class FlyerUploadPage extends StatefulWidget {
  const FlyerUploadPage({super.key});

  @override
  State<FlyerUploadPage> createState() => _FlyerUploadPageState();
}

class _FlyerUploadPageState extends State<FlyerUploadPage> {
  File? _imageFile;
  Uint8List? _webImageBytes; // Web対応用
  final ImagePicker _picker = ImagePicker();
  
  bool _isLoading = false;
  String _resultMessage = "";
  Map<String, dynamic>? _extractedData;

  // Renderで公開したご自身のAPIのURLに書き換えてください
  final String _apiUrl = "https://flyer-api-is88.onrender.com/extract";

  // 1. ギャラリーまたはカメラから画像を選択
  Future<void> _pickImage(ImageSource source) async {
    final XFile? pickedFile = await _picker.pickImage(source: source);
    if (pickedFile != null) {
      if (kIsWeb) {
        var bytes = await pickedFile.readAsBytes();
        setState(() {
          _webImageBytes = bytes;
          _resultMessage = "";
          _extractedData = null;
        });
      } else {
        setState(() {
          _imageFile = File(pickedFile.path);
          _resultMessage = "";
          _extractedData = null;
        });
      }
    }
  }

  // 2. バックエンドAPIへ画像をアップロード
  Future<void> _uploadImage() async {
    if (_imageFile == null && _webImageBytes == null) {
      setState(() {
        _resultMessage = "先に画像を選択してください。";
      });
      return;
    }

    setState(() {
      _isLoading = true;
      _resultMessage = "AIがチラシを解析中...";
      _extractedData = null;
    });

    try {
      var request = http.MultipartRequest('POST', Uri.parse(_apiUrl));

      if (kIsWeb) {
        request.files.add(
          http.MultipartFile.fromBytes('file', _webImageBytes!, filename: 'flyer.jpg'),
        );
      } else {
        request.files.add(
          await http.MultipartFile.fromPath('file', _imageFile!.path),
        );
      }

      var streamedResponse = await request.send();
      var response = await http.Response.fromStream(streamedResponse);

      if (response.statusCode == 200) {
        final data = jsonDecode(utf8.decode(response.bodyBytes));
        setState(() {
          _resultMessage = data['message'];
          _extractedData = data['data'];
        });
      } else {
        final errorData = jsonDecode(utf8.decode(response.bodyBytes));
        setState(() {
          _resultMessage = "エラーが発生しました: ${errorData['detail'] ?? response.statusCode}";
        });
      }
    } catch (e) {
      setState(() {
        _resultMessage = "通信エラー: $e";
      });
    } finally {
      setState(() {
        _isLoading = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('チラシ価格データ抽出')),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16.0),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            // 画像プレビュー表示エリア
            Container(
              height: 250,
              decoration: BoxDecoration(
                border: Border.all(color: Colors.grey),
                borderRadius: BorderRadius.circular(8),
              ),
              child: (_imageFile == null && _webImageBytes == null)
                  ? const Center(child: Text('画像が選択されていません'))
                  : (kIsWeb
                      ? Image.memory(_webImageBytes!, fit: BoxFit.cover)
                      : Image.file(_imageFile!, fit: BoxFit.cover)),
            ),
            const SizedBox(height: 12),

            // 画像選択ボタン（カメラ・ギャラリー）
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceEvenly,
              children: [
                ElevatedButton.icon(
                  onPressed: () => _pickImage(ImageSource.camera),
                  icon: const Icon(Icons.camera_alt),
                  label: const Text('写真を撮る'),
                ),
                ElevatedButton.icon(
                  onPressed: () => _pickImage(ImageSource.gallery),
                  icon: const Icon(Icons.photo_library),
                  label: const Text('アルバムから選ぶ'),
                ),
              ],
            ),
            const SizedBox(height: 20),

            // 解析・保存実行ボタン
            ElevatedButton(
              style: ElevatedButton.styleFrom(
                backgroundColor: Colors.green,
                foregroundColor: Colors.white,
                padding: const EdgeInsets.symmetric(vertical: 14),
              ),
              onPressed: _isLoading ? null : _uploadImage,
              child: _isLoading
                  ? const CircularProgressIndicator(color: Colors.white)
                  : const Text('AI解析してDBに保存する', style: TextStyle(fontSize: 16)),
            ),
            const SizedBox(height: 20),

            // 結果表示エリア
            Text(
              _resultMessage,
              style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 16),
            ),
            const SizedBox(height: 10),

            // 抽出された商品データのリスト表示
            if (_extractedData != null) ...[
              Text(
                "店舗名: ${_extractedData!['store_name']}",
                style: const TextStyle(fontSize: 18, fontWeight: FontWeight.bold, color: Colors.blue),
              ),
              const SizedBox(height: 10),
              ListView.builder(
                shrinkWrap: true,
                physics: const NeverScrollableScrollPhysics(),
                itemCount: _extractedData!['items'].length,
                itemBuilder: (context, index) {
                  final item = _extractedData!['items'][index];
                  return Card(
                    margin: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                    child: ListTile(
                      title: Text(item['product_name'] ?? ''),
                      subtitle: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          if (item['manufacturer'] != null && item['manufacturer'].toString().isNotEmpty)
                            Text('メーカー: ${item['manufacturer']}'),
                          Text('容量: ${item['capacity'] ?? "なし"}'),
                          if (item['start_date'] != null || item['end_date'] != null)
                            Text('期間: ${item['start_date'] ?? ""} 〜 ${item['end_date'] ?? ""}'),
                        ], // ← Columnの children の閉じ括弧
                      ),    // ← Column の閉じ括弧
                      trailing: Text(
                        '${item['price']}円',
                        style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold, color: Colors.red),
                      ),
                    ), // ← ListTile の閉じ括弧
                  );   // ← ★ここ重要：Card の閉じ括弧 (ここで `);` になっているか確認)
                },     // ← itemBuilder の閉じ括弧とカンマ
              ),
            ],
          ],
        ),
      ),
    );
  }
}