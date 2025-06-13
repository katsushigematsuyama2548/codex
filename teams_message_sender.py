import urllib3
import json
import os
import boto3

# Clients
http = urllib3.PoolManager()
ssm_client = boto3.client('ssm')

# Env variables
TENANT_ID = os.environ['TENANT_ID']
CLIENT_ID = os.environ['CLIENT_ID']
CLIENT_SECRET = os.environ['CLIENT_SECRET']
DEFAULT_TEAM_ID = os.environ['TEMA_ID']
DEFAULT_CHANNEL_ID = os.environ['CHANNEL_ID']
REFRESH_TOKEN_PARAM_NAME = "/teams/refresh_token"

# ========== Error Map ==========
# 301 - Token \u30ea\u30d5\u30ec\u30c3\u30b7\u30e5\u30a8\u30e9\u30fc
# 401 - SSM get_parameter \u5931\u6557
# 402 - SSM put_parameter \u5931\u6557
# 303 - Teams \u30e1\u30c3\u30bb\u30fc\u30b8\u6295\u7a3f\u5931\u6557
# 404 - \u30e6\u30fc\u30b6\u30fc\u898b\u3064\u304b\u3089\u306a\u3044
# 405 - \u30c1\u30e3\u30c3\u30c8\u4f5c\u6210\u5931\u6557
# 406 - \u30c1\u30e3\u30c3\u30c8\u30e1\u30c3\u30bb\u30fc\u30b8\u9001\u4fe1\u5931\u6557
# 500 - \u305d\u306e\u4ed6\u4e88\u671f\u305b\u306c\u30a8\u30e9\u30fc
# ===============================

# MyAppException
class MyAppException(Exception):
    def __init__(self, status_code, message):
        self.status_code = status_code
        self.message = message
        super().__init__(self.message)

# Lambda handler
def lambda_handler(event, context):
    try:
        # Refresh token
        refresh_token = get_refresh_token_from_ssm(REFRESH_TOKEN_PARAM_NAME)
        access_token, new_refresh_token = refresh_access_token(refresh_token)

        if new_refresh_token and new_refresh_token != refresh_token:
            save_refresh_token_to_ssm(new_refresh_token, REFRESH_TOKEN_PARAM_NAME)

        # Request body
        body = json.loads(event.get("body", "{}"))
        mode = body.get("mode", 1)
        email_address = body.get("email_address")
        team_id = body.get("team_id", DEFAULT_TEAM_ID)
        channel_id = body.get("channel_id", DEFAULT_CHANNEL_ID)
        message_text = body["message_text"]
        content_type = body.get("content_type", "text")
        subject = body.get("subject", "")
        mentions = body.get("mentions", [])

        # Validation (\u5168\u90e8 0 \u306e\u5834\u5408\u306f\u9001\u4fe1\u3057\u306a\u3044)
        if team_id == "0" and channel_id == "0" and message_text == "0":
            print("Skipping message send because team_id/channel_id/message_text are all '0'")
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': 'Skip send due to empty input'
                })
            }

        # \u500b\u5225\u30c1\u30e3\u30c3\u30c8\u9001\u4fe1\u306e\u5834\u5408
        if email_address:
            print(f"\ud83d\udce7 Sending message to individual chat: {email_address}")
            
            # \u30e6\u30fc\u30b6\u30fc\u3092\u691c\u7d22
            user_info = find_user_by_email(access_token, email_address)
            
            # \u65e2\u5b58\u30c1\u30e3\u30c3\u30c8\u3092\u691c\u7d22\u307e\u305f\u306f\u65b0\u898f\u4f5c\u6210
            chat_id = find_or_create_chat(access_token, user_info["id"])
            
            # \u30c1\u30e3\u30c3\u30c8\u306b\u30e1\u30c3\u30bb\u30fc\u30b8\u3092\u9001\u4fe1
            post_message_to_chat(
                access_token,
                chat_id,
                message_text,
                content_type,
                subject,
                mentions
            )
            
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': f'Message sent to chat with {email_address}'
                })
            }
        
        # \u5fb4\u6765\u306e\u30c1\u30e3\u30f3\u30cd\u30eb\u9001\u4fe1
        else:
            # Mention type \u30c1\u30a7\u30c3\u30af
            mention_types_in_request = set(
                mention.get("mention_type") for mention in mentions if "mention_type" in mention
            )

            if len(mention_types_in_request) > 1:
                raise MyAppException(303, "Mixed mention_type is not allowed. Please use 'user' or no mention only.")

            if "tag" in mention_types_in_request:
                raise MyAppException(303, "Tag mentions are currently not supported. Please use 'user' mentions only.")

            # Post message (user mention or no mention only)
            post_message_standard(
                access_token,
                team_id,
                channel_id,
                message_text,
                content_type,
                subject,
                mentions
            )

            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': 'Message posted successfully'
                })
            }

    except MyAppException as e:
        print(f"[ERROR] {e.status_code}: {e.message}")
        return {
            'statusCode': e.status_code,
            'body': json.dumps({
                'message': f'{e.message}'
            })
        }
    except Exception as e:
        print(f"[ERROR] Unexpected: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({
                'message': f'Internal error: {str(e)}'
            })
        }

# Token refresh
def refresh_access_token(refresh_token):
    token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "client_id": CLIENT_ID,
        "scope": "ChannelMessage.Send Chat.ReadWrite offline_access User.Read.All",
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        "client_secret": CLIENT_SECRET
    }

    try:
        encoded_data = urllib3.request.urlencode(data)
        response = http.request("POST", token_url, body=encoded_data, headers=headers)
        token_response = json.loads(response.data.decode())
        print("Refresh token response status:", response.status)

        if response.status not in [200]:
            raise MyAppException(301, f"Token refresh failed: {response.status} {response.data.decode()}")

        return token_response.get("access_token"), token_response.get("refresh_token")

    except Exception as e:
        raise MyAppException(301, f"Token refresh error: {str(e)}")

# Find user by email
def find_user_by_email(access_token, email_address):
    url = f"https://graph.microsoft.com/v1.0/users/{email_address}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    
    try:
        response = http.request("GET", url, headers=headers)
        if response.status == 404:
            raise MyAppException(404, f"User not found: {email_address}")
        elif response.status not in [200]:
            raise MyAppException(404, f"Failed to find user: {response.status} {response.data.decode()}")
        
        user_data = json.loads(response.data.decode())
        print(f"\u2705 Found user: {user_data.get('displayName')} ({user_data.get('id')})")
        return user_data
        
    except Exception as e:
        if isinstance(e, MyAppException):
            raise
        raise MyAppException(404, f"User search error: {str(e)}")

# Find existing chat or create new one
def find_or_create_chat(access_token, target_user_id):
    # \u307e\u305a\u65e2\u5b58\u306e\u30c1\u30e3\u30c3\u30c8\u3092\u691c\u7d22
    url = "https://graph.microsoft.com/v1.0/me/chats"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    
    try:
        response = http.request("GET", url, headers=headers)
        if response.status == 200:
            chats_data = json.loads(response.data.decode())
            
            # 1:1\u30c1\u30e3\u30c3\u30c8\u3067\u5bfe\u8c61\u30e6\u30fc\u30b6\u30fc\u3068\u306e\u30c1\u30e3\u30c3\u30c8\u3092\u691c\u7d22
            for chat in chats_data.get("value", []):
                if chat.get("chatType") == "oneOnOne":
                    members = chat.get("members", [])
                    if len(members) == 2:
                        for member in members:
                            user_info = member.get("user", {})
                            if user_info.get("id") == target_user_id:
                                print(f"\u2705 Found existing chat: {chat['id']}")
                                return chat["id"]
        
        # \u65e2\u5b58\u30c1\u30e3\u30c3\u30c8\u304c\u898b\u3064\u304b\u306a\u3044\u5834\u5408\u306f\u65b0\u898f\u4f5c\u6210
        return create_new_chat(access_token, target_user_id)
        
    except Exception as e:
        print(f"\u26a0\ufe0f Error searching existing chats: {str(e)}")
        # \u691c\u7d22\u306b\u5931\u6557\u3057\u305f\u5834\u5408\u3082\u65b0\u898f\u4f5c\u6210\u3092\u8a66\u884c
        return create_new_chat(access_token, target_user_id)

# Create new chat
def create_new_chat(access_token, target_user_id):
    # 1) \u547c\u3073\u51fa\u3057\u5143\u30e6\u30fc\u30b6\u30fcID\u3092\u53d6\u5f97
    resp_me = http.request(
        "GET",
        "https://graph.microsoft.com/v1.0/me",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    me = json.loads(resp_me.data.decode())
    my_user_id = me["id"]

    # 2) 2\u4eba\u5206\u306e\u30e1\u30f3\u30d0\u30fc\u3092\u6307\u5b9a\u3057\u3066\u30c1\u30e3\u30c3\u30c8\u3092\u4f5c\u6210
    url = "https://graph.microsoft.com/v1.0/chats"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
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
    response = http.request(
        "POST", url,
        body=json.dumps(body).encode("utf-8"),
        headers=headers
    )
    if response.status not in (200, 201):
        raise MyAppException(
            405,
            f"Chat creation failed: {response.status} {response.data.decode()}"
        )
    return json.loads(response.data.decode())["id"]

# Post message to chat
def post_message_to_chat(access_token, chat_id, message_text, content_type, subject, mentions_param):
    url = f"https://graph.microsoft.com/v1.0/chats/{chat_id}/messages"
    
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

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    
    body = {
        "body": {
            "contentType": content_type,
            "content": mention_text_in_body
        }
    }
    
    # \u30c1\u30e3\u30c3\u30c8\u3067\u306f\u4ef6\u540d\u306f\u4f7f\u7528\u3057\u306a\u3044\uff08\u30c1\u30e3\u30f3\u30cd\u30eb\u3068\u306f\u7570\u306a\u308b\uff09
    if mentions:
        body["mentions"] = mentions

    try:
        response = http.request("POST", url, body=json.dumps(body).encode("utf-8"), headers=headers)
        if response.status not in [200, 201]:
            raise MyAppException(406, f"Chat message post failed: {response.status} {response.data.decode()}")

        print("\u2705 Message posted to chat:", response.data.decode())
    except Exception as e:
        if isinstance(e, MyAppException):
            raise
        raise MyAppException(406, f"Chat message post error: {str(e)}")

# Post message (Standard for user mention or no mention)
def post_message_standard(access_token, team_id, channel_id, message_text, content_type, subject, mentions_param):
    url = f"https://graph.microsoft.com/v1.0/teams/{team_id}/channels/{channel_id}/messages"

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

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    body = {
        "subject": subject,
        "body": {
            "contentType": content_type,
            "content": mention_text_in_body
        }
    }
    if mentions:
        body["mentions"] = mentions

    try:
        response = http.request("POST", url, body=json.dumps(body).encode("utf-8"), headers=headers)
        if response.status not in [200, 201]:
            raise MyAppException(303, f"Message post failed: {response.status} {response.data.decode()}")

        print("\u2705 Message posted (standard):", response.data.decode())
    except Exception as e:
        raise MyAppException(303, f"Message post error: {str(e)}")

# SSM GET
def get_refresh_token_from_ssm(param_name):
    try:
        response = ssm_client.get_parameter(Name=param_name, WithDecryption=True)
        refresh_token = response['Parameter']['Value']
        print("\u2705 Retrieved refresh_token from SSM")
        return refresh_token
    except Exception as e:
        raise MyAppException(401, f"SSM get_parameter error: {str(e)}")

# SSM PUT
def save_refresh_token_to_ssm(refresh_token, param_name):
    try:
        ssm_client.put_parameter(
            Name=param_name,
            Value=refresh_token,
            Type="SecureString",
            Overwrite=True
        )
        print(f"\u2705 Saved refresh_token to SSM Parameter Store ({param_name})")
    except Exception as e:
        raise MyAppException(402, f"SSM put_parameter error: {str(e)}")
