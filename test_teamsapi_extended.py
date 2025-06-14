import pytest
import json
import urllib3
import os
from unittest.mock import Mock, patch, MagicMock
from pydantic import ValidationError

# 環境変数をモック化（teamsapiインポート前に設定）
os.environ.setdefault('TENANT_ID', 'test-tenant-id')
os.environ.setdefault('CLIENT_ID', 'test-client-id')
os.environ.setdefault('CLIENT_SECRET', 'test-client-secret')
os.environ.setdefault('REFRESH_TOKEN_PARAM_NAME', '/test/refresh_token')

# テスト対象のインポート
from teamsapi import (
    make_graph_request,
    refresh_access_token,
    find_user_by_email,
    find_team_id_by_name,
    find_channel_id_by_name,
    find_or_create_chat,
    create_new_chat,
    post_message_to_chat,
    post_message_to_channel,
    get_refresh_token_from_ssm,
    save_refresh_token_to_ssm,
    build_mentions_for_message,
    process_mentions_by_email,
    handle_refresh_token_mode,
    handle_dm_mode,
    handle_channel_mode,
    lambda_handler,
    APIException,
    ExternalAPIException,
    DMRequestModel,
    ChannelRequestModel,
    RefreshTokenRequestModel,
    MentionModel
)

"""
=============================================================================
Microsoft Teams API Lambda Function 拡張テストスイート

このファイルは、teamsapi.pyの各関数を詳細にテストする拡張テストスイートです。
関数ごとにクラス分けし、成功ケースと失敗ケースを明確に分けています。

テスト構成:
- 各関数ごとに独立したテストクラス
- 成功ケース（Success）と失敗ケース（Failure）を分離
- 詳細な説明文章とテスト目的の明記
- 境界値テストとエラーハンドリングの包括的検証
=============================================================================
"""

# =============================================================================
# Microsoft Graph API通信関数テスト
# 
# make_graph_request 関数をテストします。
# この関数は全てのMicrosoft Graph API呼び出しの基盤となる重要な関数です。
# HTTPリクエスト、レスポンス処理、エラーハンドリングを包括的にテストします。
# =============================================================================

class TestMakeGraphRequestSuccess:
    """make_graph_request関数の成功ケーステスト
    
    正常なGraph API通信が正しく処理されることを確認します。
    - GET/POSTリクエストの成功
    - レスポンスデータの正しい解析
    - 様々なHTTP成功ステータスコード
    """
    
    @patch('teamsapi.http')
    def test_成功ケース_GETリクエスト(self, mock_http):
        """成功ケース: GETリクエストの正常処理
        
        GETリクエストが正しく送信され、レスポンスが適切に解析されることを確認します。
        - 正しいURL構築
        - 適切なヘッダー設定
        - JSONレスポンスの解析
        """
        # モック設定
        mock_response = Mock()
        mock_response.status = 200
        mock_response.data.decode.return_value = '{"id": "user123", "displayName": "テストユーザー"}'
        mock_http.request.return_value = mock_response
        
        result = make_graph_request("GET", "/users/test@example.com", "access_token", request_id="req-123")
        
        # 結果検証
        assert result["id"] == "user123"
        assert result["displayName"] == "テストユーザー"
        
        # HTTP呼び出し検証
        mock_http.request.assert_called_once()
        call_args = mock_http.request.call_args
        assert call_args[0][0] == "GET"  # method
        assert "https://graph.microsoft.com/v1.0/users/test@example.com" in call_args[0][1]  # URL
        assert "Authorization" in call_args[1]["headers"]
        assert "Bearer access_token" in call_args[1]["headers"]["Authorization"]
    
    @patch('teamsapi.http')
    def test_成功ケース_POSTリクエスト(self, mock_http):
        """成功ケース: POSTリクエストの正常処理
        
        POSTリクエストが正しく送信され、リクエストボディが適切に処理されることを確認します。
        - リクエストボディのJSON化
        - UTF-8エンコーディング
        - 日本語データの正しい処理
        """
        # モック設定
        mock_response = Mock()
        mock_response.status = 201
        mock_response.data.decode.return_value = '{"id": "message123", "body": {"content": "送信完了"}}'
        mock_http.request.return_value = mock_response
        
        request_body = {"body": {"content": "テストメッセージ", "contentType": "text"}}
        result = make_graph_request("POST", "/chats/chat123/messages", "access_token", request_body, "req-123")
        
        # 結果検証
        assert result["id"] == "message123"
        assert result["body"]["content"] == "送信完了"
        
        # HTTP呼び出し検証
        call_args = mock_http.request.call_args
        assert call_args[0][0] == "POST"
        assert "body" in call_args[1]  # リクエストボディが含まれている

class TestMakeGraphRequestFailure:
    """make_graph_request関数の失敗ケーステスト
    
    様々なエラー状況が適切に処理されることを確認します。
    - HTTPエラーステータス
    - ネットワークエラー
    - JSONパースエラー
    - パラメータエラー
    """
    
    @patch('teamsapi.http')
    def test_失敗ケース_401認証エラー(self, mock_http):
        """失敗ケース: 401認証エラー
        
        認証エラーが適切にExternalAPIException(401)として処理されることを確認します。
        - 正しいステータスコード
        - 外部APIエラー情報の保持
        """
        mock_response = Mock()
        mock_response.status = 401
        mock_response.data.decode.return_value = '{"error": {"message": "Invalid access token"}}'
        mock_http.request.return_value = mock_response
        
        with pytest.raises(ExternalAPIException) as exc_info:
            make_graph_request("GET", "/users/test@example.com", "invalid_token", request_id="req-123")
        
        assert exc_info.value.status_code == 401
        assert "External API: 401 - Invalid access token" in exc_info.value.message
        assert exc_info.value.external_status == 401
        assert exc_info.value.external_message == "Invalid access token"
    
    @patch('teamsapi.http')
    def test_失敗ケース_404リソース未発見(self, mock_http):
        """失敗ケース: 404リソース未発見エラー
        
        リソース未発見エラーが適切にExternalAPIException(404)として処理されることを確認します。
        """
        mock_response = Mock()
        mock_response.status = 404
        mock_response.data.decode.return_value = '{"error": {"message": "User not found"}}'
        mock_http.request.return_value = mock_response
        
        with pytest.raises(ExternalAPIException) as exc_info:
            make_graph_request("GET", "/users/notfound@example.com", "access_token", request_id="req-123")
        
        assert exc_info.value.status_code == 404
        assert "External API: 404 - User not found" in exc_info.value.message
    
    def test_失敗ケース_request_id必須(self):
        """失敗ケース: request_id必須パラメータ
        
        request_idが指定されていない場合のエラー処理を確認します。
        ログ出力のためにrequest_idは必須パラメータです。
        """
        with pytest.raises(ValueError) as exc_info:
            make_graph_request("GET", "/test", "token")  # request_id不足
        
        assert "request_id is required" in str(exc_info.value)

# =============================================================================
# OAuth2トークン管理関数テスト
# 
# refresh_access_token 関数をテストします。
# この関数はMicrosoft OAuth2トークンのリフレッシュを行う重要な関数です。
# 認証エラー、ネットワークエラー、レスポンス解析エラーを包括的にテストします。
# =============================================================================

class TestRefreshAccessTokenSuccess:
    """refresh_access_token関数の成功ケーステスト
    
    正常なトークンリフレッシュが正しく処理されることを確認します。
    - 新しいアクセストークンの取得
    - 新しいリフレッシュトークンの取得
    - 適切なリクエスト形式
    """
    
    @patch('teamsapi.http')
    def test_成功ケース_トークンリフレッシュ(self, mock_http):
        """成功ケース: 正常なトークンリフレッシュ
        
        有効なリフレッシュトークンで新しいアクセストークンが正しく取得されることを確認します。
        - 正しいPOSTリクエスト
        - 適切なリクエストボディ
        - トークンレスポンスの解析
        """
        mock_response = Mock()
        mock_response.status = 200
        mock_response.data.decode.return_value = json.dumps({
            "access_token": "new_access_token_123",
            "refresh_token": "new_refresh_token_456",
            "token_type": "Bearer",
            "expires_in": 3600
        })
        mock_http.request.return_value = mock_response
        
        access_token, refresh_token = refresh_access_token("old_refresh_token", "req-123")
        
        # 結果検証
        assert access_token == "new_access_token_123"
        assert refresh_token == "new_refresh_token_456"
        
        # HTTP呼び出し検証
        mock_http.request.assert_called_once()
        call_args = mock_http.request.call_args
        assert call_args[0][0] == "POST"
        assert "login.microsoftonline.com" in call_args[0][1]
        assert "refresh_token=old_refresh_token" in call_args[1]["body"]

class TestRefreshAccessTokenFailure:
    """refresh_access_token関数の失敗ケーステスト
    
    様々なエラー状況が適切に処理されることを確認します。
    - 無効なリフレッシュトークン
    - ネットワークエラー
    - JSONパースエラー
    - HTTPエラーステータス
    """
    
    @patch('teamsapi.http')
    def test_失敗ケース_無効リフレッシュトークン(self, mock_http):
        """失敗ケース: 無効なリフレッシュトークン
        
        無効なリフレッシュトークンが適切にAPIException(401)として処理されることを確認します。
        - 401ステータスコードの処理
        - 適切なエラーメッセージ
        """
        mock_response = Mock()
        mock_response.status = 401
        mock_response.data.decode.return_value = '{"error": "invalid_grant"}'
        mock_http.request.return_value = mock_response
        
        with pytest.raises(APIException) as exc_info:
            refresh_access_token("invalid_refresh_token", "req-123")
        
        assert exc_info.value.status_code == 401
        assert "Invalid refresh token" in exc_info.value.message
    
    @patch('teamsapi.http')
    def test_失敗ケース_様々なHTTPエラー(self, mock_http):
        """失敗ケース: 様々なHTTPエラーステータス
        
        400, 403, 500などのHTTPエラーが適切にAPIException(502)として処理されることを確認します。
        """
        error_cases = [
            (400, "Bad Request"),
            (403, "Forbidden"),
            (500, "Internal Server Error"),
        ]
        
        for status_code, error_message in error_cases:
            mock_response = Mock()
            mock_response.status = status_code
            mock_response.data.decode.return_value = f'{{"error": "{error_message}"}}'
            mock_http.request.return_value = mock_response
            
            with pytest.raises(APIException) as exc_info:
                refresh_access_token("test_token", "req-123")
            
            assert exc_info.value.status_code == 502
            assert f"Token refresh failed {status_code}" in exc_info.value.message
    
    @patch('teamsapi.http')
    def test_失敗ケース_JSONパースエラー(self, mock_http):
        """失敗ケース: JSONパースエラー
        
        トークンレスポンスのJSON解析に失敗した場合のエラー処理を確認します。
        """
        mock_response = Mock()
        mock_response.status = 200
        mock_response.data.decode.return_value = 'invalid json response'
        mock_http.request.return_value = mock_response
        
        with pytest.raises(APIException) as exc_info:
            refresh_access_token("test_token", "req-123")
        
        assert exc_info.value.status_code == 500
        assert "Failed to parse token response" in exc_info.value.message

# =============================================================================
# ユーザー検索関数テスト
# 
# find_user_by_email 関数をテストします。
# この関数はメールアドレスからユーザー情報を取得する重要な関数です。
# Graph APIを使用してユーザー検索を行います。
# =============================================================================

class TestFindUserByEmailSuccess:
    """find_user_by_email関数の成功ケーステスト
    
    正常なユーザー検索が正しく処理されることを確認します。
    - メールアドレスからユーザー情報取得
    - 日本語ユーザー名の処理
    - 様々なメール形式の対応
    """
    
    @patch('teamsapi.make_graph_request')
    def test_成功ケース_ユーザー検索(self, mock_graph_request):
        """成功ケース: メールアドレスからユーザー情報取得
        
        有効なメールアドレスでユーザー情報が正しく取得されることを確認します。
        - 正しいGraph APIエンドポイント呼び出し
        - ユーザー情報の返却
        - 日本語ユーザー名の処理
        """
        mock_graph_request.return_value = {
            "id": "user123",
            "displayName": "田中太郎",
            "mail": "tanaka@example.com",
            "userPrincipalName": "tanaka@example.com"
        }
        
        result = find_user_by_email("access_token", "tanaka@example.com", "req-123")
        
        # 結果検証
        assert result["id"] == "user123"
        assert result["displayName"] == "田中太郎"
        assert result["mail"] == "tanaka@example.com"
        
        # Graph API呼び出し検証
        mock_graph_request.assert_called_once_with(
            "GET", "/users/tanaka@example.com", "access_token", request_id="req-123"
        )

class TestFindUserByEmailFailure:
    """find_user_by_email関数の失敗ケーステスト
    
    様々なエラー状況が適切に処理されることを確認します。
    - ユーザー未発見
    - 権限エラー
    - ネットワークエラー
    """
    
    @patch('teamsapi.make_graph_request')
    def test_失敗ケース_ユーザー未発見(self, mock_graph_request):
        """失敗ケース: ユーザー未発見
        
        存在しないメールアドレスが適切にAPIException(404)として処理されることを確認します。
        - 404エラーの適切な処理
        - カスタムエラーメッセージ
        """
        mock_graph_request.side_effect = APIException(404, "Resource not found")
        
        with pytest.raises(APIException) as exc_info:
            find_user_by_email("access_token", "notfound@example.com", "req-123")
        
        assert exc_info.value.status_code == 404
        assert "User not found: notfound@example.com" in exc_info.value.message
    
    @patch('teamsapi.make_graph_request')
    def test_失敗ケース_その他のAPIエラー(self, mock_graph_request):
        """失敗ケース: その他のAPIエラー
        
        404以外のAPIエラーがそのまま再発生することを確認します。
        - エラーの透過的な伝播
        - 元のエラー情報の保持
        """
        mock_graph_request.side_effect = APIException(502, "Bad Gateway")
        
        with pytest.raises(APIException) as exc_info:
            find_user_by_email("access_token", "test@example.com", "req-123")
        
        assert exc_info.value.status_code == 502
        assert exc_info.value.message == "Bad Gateway"

# =============================================================================
# チーム検索関数テスト
# 
# find_team_id_by_name 関数をテストします。
# この関数はチーム名からチームIDを取得する重要な関数です。
# ユーザーが参加しているチームの一覧から名前で検索します。
# =============================================================================

class TestFindTeamIdByNameSuccess:
    """find_team_id_by_name関数の成功ケーステスト
    
    正常なチーム検索が正しく処理されることを確認します。
    - チーム名からチームID取得
    - 日本語チーム名の処理
    - 複数チームからの検索
    """
    
    @patch('teamsapi.make_graph_request')
    def test_成功ケース_チーム検索(self, mock_graph_request):
        """成功ケース: チーム名からチームID取得
        
        有効なチーム名でチームIDが正しく取得されることを確認します。
        - 正しいGraph APIエンドポイント呼び出し
        - チーム一覧からの名前検索
        - 日本語チーム名の処理
        """
        mock_graph_request.return_value = {
            "value": [
                {"id": "team123", "displayName": "開発チーム"},
                {"id": "team456", "displayName": "営業チーム"},
                {"id": "team789", "displayName": "マーケティングチーム"}
            ]
        }
        
        result = find_team_id_by_name("access_token", "開発チーム", "req-123")
        
        # 結果検証
        assert result == "team123"
        
        # Graph API呼び出し検証
        mock_graph_request.assert_called_once_with(
            "GET", "/me/joinedTeams", "access_token", request_id="req-123"
        )

class TestFindTeamIdByNameFailure:
    """find_team_id_by_name関数の失敗ケーステスト
    
    様々なエラー状況が適切に処理されることを確認します。
    - チーム未発見
    - 空のチーム一覧
    - APIエラー
    """
    
    @patch('teamsapi.make_graph_request')
    def test_失敗ケース_チーム未発見(self, mock_graph_request):
        """失敗ケース: チーム未発見
        
        存在しないチーム名が適切にAPIException(404)として処理されることを確認します。
        - 404エラーの生成
        - カスタムエラーメッセージ
        """
        mock_graph_request.return_value = {
            "value": [
                {"id": "team456", "displayName": "営業チーム"}
            ]
        }
        
        with pytest.raises(APIException) as exc_info:
            find_team_id_by_name("access_token", "存在しないチーム", "req-123")
        
        assert exc_info.value.status_code == 404
        assert "Team not found: 存在しないチーム" in exc_info.value.message
    
    @patch('teamsapi.make_graph_request')
    def test_失敗ケース_空のチーム一覧(self, mock_graph_request):
        """失敗ケース: 空のチーム一覧
        
        ユーザーがどのチームにも参加していない場合のエラー処理を確認します。
        """
        mock_graph_request.return_value = {"value": []}
        
        with pytest.raises(APIException) as exc_info:
            find_team_id_by_name("access_token", "任意のチーム", "req-123")
        
        assert exc_info.value.status_code == 404
        assert "Team not found: 任意のチーム" in exc_info.value.message

# 実行設定
if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"]) 