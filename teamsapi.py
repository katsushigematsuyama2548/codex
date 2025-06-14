import urllib3
import urllib.parse
import json
import os
import boto3
import logging
import uuid
from typing import List, Optional, Literal, Union
from pydantic import BaseModel, EmailStr, Field, ValidationError

# ========== HTTP Status Code Based Error Handling ==========
# 400: Bad Request - バリデーション、JSONパースエラー
# 401: Unauthorized - 認証、トークン関連エラー
# 404: Not Found - ユーザー、チーム、チャンネル未発見
# 422: Unprocessable Entity - ビジネスロジック、メンション処理エラー
# 500: Internal Server Error - システム、SSM、API通信エラー
# 502: Bad Gateway - 外部API（Graph API）エラー
# 503: Service Unavailable - 一時的なサービス停止
# ===============================

# ログ設定
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# urllib3設定（タイムアウト付き）
http = urllib3.PoolManager(timeout=urllib3.Timeout(15))
ssm_client = boto3.client('ssm')


TENANT_ID = os.environ['TENANT_ID']
CLIENT_ID = os.environ['CLIENT_ID']
CLIENT_SECRET = os.environ['CLIENT_SECRET']
REFRESH_TOKEN_PARAM_NAME = os.environ['REFRESH_TOKEN_PARAM_NAME']

class APIException(Exception):
    """HTTPステータスコードベースの例外クラス
    
    Attributes:
        status_code (int): HTTPステータスコード
        message (str): エラーメッセージ
    """
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(self.message)

class ExternalAPIException(APIException):
    """外部API例外クラス（詳細情報付き）"""
    def __init__(self, status_code: int, message: str, 
                 external_status: int = None, external_message: str = None):
        if external_status and external_message:
            detailed_message = f"{message} (External API: {external_status} - {external_message})"
        else:
            detailed_message = message
        super().__init__(status_code, detailed_message)
        self.external_status = external_status
        self.external_message = external_message

def create_error_response(request_id: str, status_code: int, message: str) -> dict:
    """統一されたエラーレスポンス作成"""
    return {
        'statusCode': status_code,
        'body': json.dumps({"request_id": request_id, "message": message}, ensure_ascii=False)
    }

def create_success_response(request_id: str, data: Optional[dict] = None, message: str = "Success") -> dict:
    """統一された成功レスポンス作成"""
    body = {"request_id": request_id, "message": message}
    if data:
        body["data"] = data
        
    return {
        'statusCode': 200,
        'body': json.dumps(body, ensure_ascii=False)
    }

# ========== Pydanticモデル定義 ==========
class MentionModel(BaseModel):
    mention_type: Literal["user"] = "user"
    email_address: EmailStr
    
    class Config:
        extra = "forbid"

class BaseRequestModel(BaseModel):
    # 1:DMメッセージ
    # 2:チャンネルメッセージ
    # 3:リフレッシュトークン更新
    mode: Literal[1, 2, 3]

class DMRequestModel(BaseRequestModel):
    mode: Literal[1] = 1
    email_addresses: List[EmailStr] = Field(..., min_items=1, max_items=250)
    message_text: str = Field(..., min_length=1, max_length=28000)
    content_type: Literal["text", "html"] = "text"
    mentions: List[MentionModel] = Field(default_factory=list, max_items=50)
    
    class Config:
        extra = "forbid"

class ChannelRequestModel(BaseRequestModel):
    mode: Literal[2] = 2
    team_name: str = Field(..., min_length=1, max_length=120)
    channel_name: str = Field(..., min_length=1, max_length=50)
    message_text: str = Field(..., min_length=1, max_length=28000)
    content_type: Literal["text", "html"] = "text"
    subject: str = Field("", max_length=255)
    mentions: List[MentionModel] = Field(default_factory=list, max_items=50)

class RefreshTokenRequestModel(BaseRequestModel):
    mode: Literal[3] = 3

# ========== メインハンドラー ==========

def lambda_handler(event: dict, context) -> dict:
    """AWS Lambda メインハンドラー関数"""
    request_id = context.aws_request_id
    
    try:                
        # リクエスト開始ログ
        logger.info(f"REQUEST_START - {request_id}")
        
        # リクエストパラメータのバリデーション
        request_data = validate_and_parse_request(event.get("body", "{}"))
        logger.info(f"REQUEST_VALIDATED - Mode:{request_data.mode} - {request_id}")
        
        # モード3: リフレッシュトークン更新のみ
        if request_data.mode == 3:
            result = handle_refresh_token_mode(request_id)
            logger.info(f"REQUEST_SUCCESS - Mode:3 TokenRefresh - {request_id}")
            return result
        
        # メッセージ送信前のトークンリフレッシュ実行
        refresh_token = get_refresh_token_from_ssm(REFRESH_TOKEN_PARAM_NAME)
        access_token, new_refresh_token = refresh_access_token(refresh_token, request_id)

        if new_refresh_token and new_refresh_token != refresh_token:
            save_refresh_token_to_ssm(new_refresh_token, REFRESH_TOKEN_PARAM_NAME)
            logger.info(f"TOKEN_UPDATED - {request_id}")

        # モード1: DM送信処理
        if request_data.mode == 1:
            result = handle_dm_mode(request_data, access_token, request_id)
            logger.info(f"REQUEST_SUCCESS - Mode:1 DM - {request_id}")
            return result
        
        # モード2: チャンネル送信処理
        elif request_data.mode == 2:
            result = handle_channel_mode(request_data, access_token, request_id)
            logger.info(f"REQUEST_SUCCESS - Mode:2 Channel - {request_id}")
            return result

    except APIException as e:
        logger.error(f"API_ERROR - Status:{e.status_code} Message:{e.message} - {request_id}")
        return create_error_response(request_id, e.status_code, e.message)
    except Exception as e:
        logger.error(f"SYSTEM_ERROR - {str(e)} - {request_id}")
        raise

# ========== モード別ハンドラー関数 ==========

def handle_refresh_token_mode(request_id: str) -> dict:
    logger.info(f"TOKEN_REFRESH_START - {request_id}")
    refresh_token = get_refresh_token_from_ssm(REFRESH_TOKEN_PARAM_NAME)
    access_token, new_refresh_token = refresh_access_token(refresh_token, request_id)

    if new_refresh_token and new_refresh_token != refresh_token:
        save_refresh_token_to_ssm(new_refresh_token, REFRESH_TOKEN_PARAM_NAME)
        logger.info(f"TOKEN_UPDATED - {request_id}")

    return create_success_response(request_id, message="Refresh token updated successfully")

def handle_dm_mode(request_data: DMRequestModel, access_token: str, request_id: str) -> dict:
    """モード1: DM送信処理"""
    logger.info(f"DM_SEND_START - Recipients:{len(request_data.email_addresses)} - {request_id}")
    
    processed_mentions = process_mentions_by_email(access_token, request_data.mentions, request_id)
    
    # 指定された各ユーザーに個別DM送信
    for email_address in request_data.email_addresses:
        user_info = find_user_by_email(access_token, email_address, request_id)
        chat_id = find_or_create_chat(access_token, user_info["id"], request_id)
        post_message_to_chat(
            access_token,
            chat_id,
            request_data.message_text,
            request_data.content_type,
            processed_mentions,
            request_id
        )
    
    logger.info(f"DM_SEND_SUCCESS - Recipients:{len(request_data.email_addresses)} - {request_id}")
    return create_success_response(request_id, message=f"Messages sent to {len(request_data.email_addresses)} users")

def handle_channel_mode(request_data: ChannelRequestModel, access_token: str, request_id: str) -> dict:
    """モード2: チャンネル送信処理"""
    logger.info(f"CHANNEL_SEND_START - Team:{request_data.team_name} Channel:{request_data.channel_name} - {request_id}")
    
    team_id = find_team_id_by_name(access_token, request_data.team_name, request_id)
    channel_id = find_channel_id_by_name(access_token, team_id, request_data.channel_name, request_id)
    processed_mentions = process_mentions_by_email(access_token, request_data.mentions, request_id)
    
    post_message_to_channel(
        access_token,
        team_id,
        channel_id,
        request_data.message_text,
        request_data.content_type,
        request_data.subject,
        processed_mentions,
        request_id
    )

    logger.info(f"CHANNEL_SEND_SUCCESS - Team:{request_data.team_name} Channel:{request_data.channel_name} - {request_id}")
    return create_success_response(request_id, message=f"Message posted to {request_data.team_name}/{request_data.channel_name}")

# ========== 共通処理関数 ==========

def validate_and_parse_request(body_json: str) -> Union[DMRequestModel, ChannelRequestModel, RefreshTokenRequestModel]:
    """リクエストボディのバリデーション・解析"""
    try:
        body = json.loads(body_json)
    except json.JSONDecodeError as e:
        raise APIException(400, f"Invalid JSON format: {str(e)}")
    
    try:
        mode = body.get("mode")
        
        if mode == 1:
            return DMRequestModel(**body)
        elif mode == 2:
            return ChannelRequestModel(**body)
        elif mode == 3:
            return RefreshTokenRequestModel(**body)
        else:
            raise APIException(400, f"Invalid mode: {mode}. Must be 1 (DM), 2 (channel), or 3 (refresh_token)")
            
    except ValidationError as e:
        raise APIException(400, f"Validation failed: {str(e)}")
    except APIException:
        raise
    except Exception as e:
        raise APIException(400, f"Request validation failed: {str(e)}")

def make_graph_request(method: str, endpoint: str, access_token: str, 
                      body: Optional[dict] = None, request_id: Optional[str] = None) -> dict:
    """Microsoft Graph APIリクエスト共通処理"""
    if request_id is None:
        raise ValueError("request_id is required for logging")
    
    url = f"https://graph.microsoft.com/v1.0{endpoint}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    
    logger.info(f"GRAPH_API_CALL - {method} {endpoint} - {request_id}")
    
    try:
        if body:
            response = http.request(method, url, headers=headers, body=json.dumps(body, ensure_ascii=False).encode("utf-8"))
        else:
            response = http.request(method, url, headers=headers)
        
        # 外部APIレスポンス解析
        try:
            response_data = json.loads(response.data.decode()) if response.data else {}
            external_message = response_data.get('error', {}).get('message', 'Unknown error')
        except:
            external_message = 'Unable to parse response'
        
        if response.status == 401:
            raise ExternalAPIException(401, "Unauthorized access", response.status, external_message)
        elif response.status == 404:
            raise ExternalAPIException(404, "Resource not found", response.status, external_message)
        elif response.status not in [200, 201]:
            raise ExternalAPIException(502, "External API error", response.status, external_message)
        
        logger.info(f"GRAPH_API_SUCCESS - Status:{response.status} - {request_id}")
        return json.loads(response.data.decode())
        
    except ExternalAPIException:
        raise
    except Exception as e:
        logger.error(f"GRAPH_API_EXCEPTION - Error:{str(e)} - {request_id}")
        raise APIException(502, f"Graph API request failed: {str(e)}")

def build_mentions_for_message(mentions_param: List[dict], message_text: str) -> tuple[List[dict], str]:
    """メンション付きメッセージの構築"""
    mentions = []
    mention_text_in_body = message_text

    for i, mention in enumerate(mentions_param):
        if mention.get("mention_type") == "user":
            mentions.append({
                "id": i,
                "mentionText": f"@{mention['display_name']}",
                "mentioned": {
                    "user": {
                        "id": mention["user_id"],
                        "displayName": mention["display_name"]
                    }
                }
            })
            mention_text_in_body += f' <at id="{i}">@{mention["display_name"]}</at>'
    
    return mentions, mention_text_in_body

def process_mentions_by_email(access_token: str, mentions_param: List[MentionModel], request_id: Optional[str] = None) -> List[dict]:
    """メールアドレスベースのメンション処理"""
    try:
        processed_mentions = []
        
        for mention in mentions_param:
            if mention.mention_type == "user" and mention.email_address:
                user_info = find_user_by_email(access_token, mention.email_address, request_id)
                
                processed_mentions.append({
                    "mention_type": "user",
                    "user_id": user_info["id"],
                    "display_name": user_info["displayName"],
                    "email_address": mention.email_address
                })
        
        if processed_mentions:
            logger.info(f"MENTIONS_PROCESSED - Count:{len(processed_mentions)} - {request_id}")
        
        return processed_mentions
        
    except APIException:
        raise
    except Exception as e:
        raise APIException(422, f"Mention processing failed: {str(e)}")

# ========== API呼び出し関数 ==========

def refresh_access_token(refresh_token: str, request_id: Optional[str] = None) -> tuple[str, str]:
    """Microsoft認証トークンのリフレッシュ"""
    logger.info(f"TOKEN_API_CALL - POST /oauth2/v2.0/token - {request_id}")
    
    try:
        token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {
            "client_id": CLIENT_ID,
            "scope": "Channel.ReadBasic.All ChannelMessage.Send Chat.ReadBasic ChatMessage.Send offline_access User.ReadBasic.All",
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "client_secret": CLIENT_SECRET
        }

        encoded_data = urllib.parse.urlencode(data)
        response = http.request("POST", token_url, body=encoded_data, headers=headers)
        
        if response.status == 401:
            logger.error(f"TOKEN_API_401 - Invalid refresh token - {request_id}")
            raise APIException(401, "Invalid refresh token")
        elif response.status != 200:
            try:
                error_body = response.data.decode()
                error_message = f"Token refresh failed {response.status}: {error_body}"
            except:
                error_message = f"Token refresh failed {response.status}: Unable to decode response"
            logger.error(f"TOKEN_API_ERROR - Status:{response.status} - {request_id}")
            raise APIException(502, error_message)

        try:
            token_response = json.loads(response.data.decode())
            logger.info(f"TOKEN_API_SUCCESS - Status:{response.status} - {request_id}")
        except Exception as e:
            logger.error(f"TOKEN_API_PARSE_ERROR - {request_id}")
            raise APIException(500, f"Failed to parse token response: {str(e)}")
            
        return token_response.get("access_token"), token_response.get("refresh_token")

    except APIException:
        raise
    except Exception as e:
        logger.error(f"TOKEN_API_EXCEPTION - {str(e)} - {request_id}")
        raise APIException(502, f"Token refresh process failed: {str(e)}")

def find_user_by_email(access_token: str, email_address: str, request_id: Optional[str] = None) -> dict:
    """メールアドレスからユーザー情報を取得"""
    try:
        user_data = make_graph_request("GET", f"/users/{email_address}", access_token, request_id=request_id)
        logger.info(f"USER_FOUND - {email_address} - {request_id}")
        return user_data
    except APIException as e:
        if e.status_code == 404:
            logger.warning(f"USER_NOT_FOUND - {email_address} - {request_id}")
            raise APIException(404, f"User not found: {email_address}")
        raise

def find_team_id_by_name(access_token: str, team_name: str, request_id: Optional[str] = None) -> str:
    """チーム名からチームIDを取得"""
    teams_data = make_graph_request("GET", "/me/joinedTeams", access_token, request_id=request_id)
    
    for team in teams_data.get("value", []):
        if team.get("displayName") == team_name:
            logger.info(f"TEAM_FOUND - {team_name} - {request_id}")
            return team["id"]
    
    logger.warning(f"TEAM_NOT_FOUND - {team_name} - {request_id}")
    raise APIException(404, f"Team not found: {team_name}")

def find_channel_id_by_name(access_token: str, team_id: str, channel_name: str, request_id: Optional[str] = None) -> str:
    """チャンネル名からチャンネルIDを取得"""
    channels_data = make_graph_request("GET", f"/teams/{team_id}/channels", access_token, request_id=request_id)
    
    for channel in channels_data.get("value", []):
        if channel.get("displayName") == channel_name:
            logger.info(f"CHANNEL_FOUND - {channel_name} - {request_id}")
            return channel["id"]
    
    logger.warning(f"CHANNEL_NOT_FOUND - {channel_name} - {request_id}")
    raise APIException(404, f"Channel not found: {channel_name}")

def find_or_create_chat(access_token: str, target_user_id: str, request_id: str) -> str:
    """1対1チャットの検索または作成"""
    try:
        chats_data = make_graph_request("GET", "/me/chats", access_token, request_id=request_id)
        
        # 既存の1:1チャットを検索
        for chat in chats_data.get("value", []):
            if chat.get("chatType") == "oneOnOne":
                members = chat.get("members", [])
                if len(members) == 2:
                    for member in members:
                        user_info = member.get("user", {})
                        if user_info.get("id") == target_user_id:
                            logger.info(f"CHAT_FOUND - Existing chat - {request_id}")
                            return chat["id"]
        
        # 既存チャットが見つからない場合のみ新規作成
        chat_id = create_new_chat(access_token, target_user_id, request_id)
        logger.info(f"CHAT_CREATED - New chat - {request_id}")
        return chat_id
        
    except APIException:
        raise
    except Exception as e:
        logger.error(f"CHAT_SEARCH_ERROR - {str(e)} - {request_id}")
        raise APIException(502, f"Chat search failed: {str(e)}")

def create_new_chat(access_token: str, target_user_id: str, request_id: Optional[str] = None) -> str:
    """新規1対1チャットの作成"""
    try:
        # 自分のユーザーID取得
        me = make_graph_request("GET", "/me", access_token, request_id=request_id)
        my_user_id = me["id"]

        # 新規チャット作成
        body = {
            "chatType": "oneOnOne",
            "members": [
                {
                    "@odata.type": "#microsoft.graph.aadUserConversationMember",
                    "roles": ["owner"],
                    "user@odata.bind": f"https://graph.microsoft.com/v1.0/users/{my_user_id}"
                },
                {
                    "@odata.type": "#microsoft.graph.aadUserConversationMember",
                    "roles": ["owner"],
                    "user@odata.bind": f"https://graph.microsoft.com/v1.0/users/{target_user_id}"
                }
            ]
        }
        
        chat_data = make_graph_request("POST", "/chats", access_token, body, request_id=request_id)
        return chat_data["id"]
        
    except APIException:
        raise
    except Exception as e:
        raise APIException(502, f"Chat creation failed: {str(e)}")

def post_message_to_chat(access_token: str, chat_id: str, message_text: str, 
                        content_type: str, mentions_param: List[dict], request_id: Optional[str] = None) -> None:
    """チャットへのメッセージ送信"""
    try:
        mentions, mention_text_in_body = build_mentions_for_message(mentions_param, message_text)
        
        body = {
            "body": {
                "contentType": content_type,
                "content": mention_text_in_body
            }
        }
        
        if mentions:
            body["mentions"] = mentions

        make_graph_request("POST", f"/chats/{chat_id}/messages", access_token, body, request_id=request_id)
        
    except APIException:
        raise
    except Exception as e:
        raise APIException(502, f"Chat message send failed: {str(e)}")

def post_message_to_channel(access_token: str, team_id: str, channel_id: str, 
                          message_text: str, content_type: str, subject: str,
                          mentions_param: List[dict], request_id: Optional[str] = None) -> None:
    """チャンネルへのメッセージ送信"""
    try:
        mentions, mention_text_in_body = build_mentions_for_message(mentions_param, message_text)

        body = {
            "subject": subject,
            "body": {
                "contentType": content_type,
                "content": mention_text_in_body
            }
        }
        
        if mentions:
            body["mentions"] = mentions

        make_graph_request("POST", f"/teams/{team_id}/channels/{channel_id}/messages", access_token, body, request_id=request_id)
        
    except APIException:
        raise
    except Exception as e:
        raise APIException(502, f"Channel message send failed: {str(e)}")

def get_refresh_token_from_ssm(param_name: str) -> str:
    """SSMパラメータストアからリフレッシュトークンを取得"""
    try:
        response = ssm_client.get_parameter(Name=param_name, WithDecryption=True)
        refresh_token = response['Parameter']['Value']
        return refresh_token
    except Exception as e:
        raise APIException(500, f"SSM parameter get failed: {str(e)}")

def save_refresh_token_to_ssm(refresh_token: str, param_name: str) -> None:
    """SSMパラメータストアにリフレッシュトークンを保存"""
    try:
        ssm_client.put_parameter(
            Name=param_name,
            Value=refresh_token,
            Type="SecureString",
            Overwrite=True
        )
    except Exception as e:
        raise APIException(500, f"SSM parameter save failed: {str(e)}")