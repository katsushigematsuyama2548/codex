import json
import boto3
import email
import urllib3
import urllib.parse
import os
import re
import logging
from email import policy
from email.parser import BytesParser
from typing import Optional, List
from pydantic import BaseModel, EmailStr, Field, ValidationError, field_validator
from datetime import datetime, timedelta, date

# ログ設定
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# urllib3設定（タイムアウト付き）
http = urllib3.PoolManager(timeout=urllib3.Timeout(30))

# Teams API設定
TEAMS_API_URL = "https://tumr4jppl1.execute-api.ap-northeast-1.amazonaws.com/dev/teams/message"

# ========== 例外クラス ==========

class APIException(Exception):
    """HTTPステータスコードベースの例外クラス"""
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

# ========== Pydanticモデル ==========

class ApprovalData(BaseModel):
    """承認データ"""
    mail: EmailStr = Field(..., description="申請者メールアドレス")
    content: str = Field(..., min_length=1, max_length=10000, description="申請内容")
    system: str = Field(..., min_length=1, max_length=200, description="申請システム")
    from_date: date = Field(..., description="ログ取得開始日")
    to_date: date = Field(..., description="ログ取得終了日")
    
    class Config:
        extra = "forbid"  # 未定義フィールド禁止
    
    @field_validator('to_date')
    @classmethod
    def validate_date_range(cls, v, info):
        """日付範囲のバリデーション"""
        if 'from_date' in info.data:
            if v < info.data['from_date']:
                raise ValueError("終了日は開始日以降の日付を指定してください")
        return v

# ========== レスポンス作成関数 ==========

def create_success_response(message: str = "Success") -> dict:
    """統一された成功レスポンス作成"""
    return {
        "statusCode": 200,
        "body": json.dumps({"message": message}, ensure_ascii=False)
    }

def create_error_response(status_code: int, message: str) -> dict:
    """統一されたエラーレスポンス作成"""
    return {
        "statusCode": status_code,
        "body": json.dumps({"message": message}, ensure_ascii=False)
    }

# ========== 1. メインハンドラー ==========

def lambda_handler(event, context):
    """AWS Lambda メインハンドラー関数"""
    sender_email = None
    mail_subject = None
    
    try:
        logger.info("REQUEST_START")
        
        # 環境変数事前チェック
        validate_environment_variables()
        
        # SESイベントからメール情報を取得
        try:
            ses_notification = event['Records'][0]['ses']
            message_id = ses_notification['mail']['messageId']
            mail_subject = ses_notification['mail']['commonHeaders']['subject']
            sender_email = ses_notification['mail']['source']
            
            logger.info(f"SES_EVENT_PARSED - MessageId:{message_id}")
        except (KeyError, IndexError) as e:
            raise APIException(400, f"SESイベント形式が不正です: {str(e)}")
        except Exception as e:
            raise APIException(400, f"SESイベントの解析に失敗しました: {str(e)}")
        
        # S3からメール本文を取得
        mail_body = get_email_body_from_s3(message_id)
        
        # 申請データ抽出・バリデーション
        approval_data = extract_and_validate_approval_data(
            mail_body, mail_subject, sender_email
        )
        
        # Teams承認メッセージ送信（承認者用）
        teams_result = send_teams_approval_message(approval_data)
        
        # Teams受付通知送信（申請者用）
        notification_result = send_teams_acceptance_notification(approval_data)
        
        logger.info("REQUEST_SUCCESS")
        return create_success_response("承認依頼を正常に送信しました")
        
    except APIException as e:
        logger.error(f"API_ERROR - Status:{e.status_code} Message:{e.message}")
        
        # エラー通知送信
        if sender_email and mail_subject:
            try:
                send_error_notification(e, sender_email, mail_subject)
            except Exception as notification_error:
                logger.error(f"ERROR_NOTIFICATION_FAILED - {str(notification_error)}")
        
        return create_error_response(e.status_code, e.message)
    except Exception as e:
        logger.error(f"SYSTEM_ERROR - {str(e)}")
        
        # 想定外エラー通知送信
        if sender_email and mail_subject:
            try:
                system_error = APIException(500, f"システムエラーが発生しました: {str(e)}")
                send_error_notification(system_error, sender_email, mail_subject)
            except Exception as notification_error:
                logger.error(f"ERROR_NOTIFICATION_FAILED - {str(notification_error)}")
        
        return create_error_response(500, f"システムエラーが発生しました: {str(e)}")

# ========== 3. 共通処理関数 ==========

def validate_environment_variables():
    """必要な環境変数の事前チェック"""
    required_vars = {
        'BUCKET_NAME': 'S3バケット名',
        'TEAMS_TEAM_NAME': 'Teamsチーム名', 
        'TEAMS_CHANNEL_NAME': 'Teamsチャンネル名',
        'ERROR_NOTIFICATION_TEAM_NAME': 'エラー通知用Teamsチーム名',
        'ERROR_NOTIFICATION_CHANNEL_NAME': 'エラー通知用Teamsチャンネル名',
        'APPROVAL_SENDER_EMAIL': '承認送信者メール'
    }
    
    missing_vars = []
    for var_name, description in required_vars.items():
        if not os.environ.get(var_name):
            missing_vars.append(f"{var_name}({description})")
    
    if missing_vars:
        raise APIException(500, f"必要な環境変数が設定されていません: {', '.join(missing_vars)}")

def extract_and_validate_approval_data(mail_body: str, subject: str, sender: str) -> ApprovalData:
    """申請データの抽出とバリデーション"""
    try:
        # 申請理由抽出
        extracted_reason = extract_reason(mail_body)
        if not extracted_reason:
            logger.warning("REASON_NOT_FOUND")
            raise APIException(400, "メール本文に【申請理由】の記載がありません。メール本文に【申請理由】[理由を記載]【ログ取得期間】の形式で記載してください。")
        
        # ログ取得期間抽出
        from_date_str, to_date_str = extract_log_period(mail_body)
        
        # 文字列をdateオブジェクトに変換
        try:
            from_date = datetime.strptime(from_date_str, "%Y-%m-%d").date()
            to_date = datetime.strptime(to_date_str, "%Y-%m-%d").date()
        except ValueError as e:
            raise APIException(400, f"日付形式が不正です: {str(e)}")
        
        # バリデーション（Pydanticが自動でバリデーション実行）
        approval_data = ApprovalData(
            mail=sender,
            content=extracted_reason,
            system=subject,
            from_date=from_date,
            to_date=to_date
        )
        
        logger.info(f"APPROVAL_DATA_VALIDATED - System:{subject} FromDate:{approval_data.from_date} ToDate:{approval_data.to_date}")
        return approval_data
        
    except ValidationError as e:
        # Pydanticバリデーションエラーを分かりやすいメッセージに変換
        error_messages = []
        for error in e.errors():
            field = error['loc'][0] if error['loc'] else 'unknown'
            message = error['msg']
            if field == 'from_date':
                error_messages.append(f"開始日: {message}")
            elif field == 'to_date':
                error_messages.append(f"終了日: {message}")
            else:
                error_messages.append(f"{field}: {message}")
        
        raise APIException(400, f"申請データのバリデーションに失敗しました: {', '.join(error_messages)}")
    except APIException:
        raise
    except Exception as e:
        raise APIException(422, f"申請データの抽出に失敗しました: {str(e)}")

def get_email_body_from_s3(message_id: str) -> str:
    """S3からメール本文を取得する"""
    s3 = boto3.client('s3')
    bucket_name = os.environ.get('BUCKET_NAME')
    
    if not bucket_name:
        raise APIException(500, "BUCKET_NAME環境変数が設定されていません")
    
    try:
        logger.info(f"S3_GET_EMAIL - Bucket:{bucket_name} Key:receive/{message_id}")
        
        response = s3.get_object(Bucket=bucket_name, Key=f"receive/{message_id}")
        raw_email = response['Body'].read()
        msg = BytesParser(policy=policy.default).parsebytes(raw_email)

        mail_body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    charset = part.get_content_charset() or 'utf-8'
                    mail_body = part.get_payload(decode=True).decode(charset)
                    break
        else:
            charset = msg.get_content_charset() or 'utf-8'
            mail_body = msg.get_payload(decode=True).decode(charset)

        logger.info(f"S3_EMAIL_SUCCESS - BodyLength:{len(mail_body)}")
        return mail_body

    except Exception as e:
        logger.error(f"S3_EMAIL_ERROR - {str(e)}")
        raise APIException(500, f"S3からのメール取得に失敗しました: {str(e)}")

def extract_reason(body_text: str) -> str:
    """メール本文から申請理由を抽出"""
    try:
        pattern = r"【申請理由】\s*(.*?)\s*【ログ取得期間】"
        match = re.search(pattern, body_text, re.DOTALL)
        if match:
            reason = match.group(1).strip()
            return reason
        return ""
    except Exception as e:
        logger.warning(f"REASON_EXTRACT_ERROR - {str(e)}")
        return ""

def extract_log_period(body_text: str) -> tuple[str, str]:
    """メール本文からログ取得期間を抽出"""
    try:
        # 【ログ取得期間】セクションを抽出
        period_section_match = re.search(r"【ログ取得期間】(.*?)(?=【|$)", body_text, re.DOTALL)
        if not period_section_match:
            raise ValueError("【ログ取得期間】セクションが見つかりません")
        
        period_section = period_section_match.group(1)
        
        # 日付パターンを抽出（実際の日付 または "yyyy-mm-dd"）
        date_patterns = [
            (r'"yyyy-mm-dd"', 'template_double'),      # "yyyy-mm-dd" (ダブルクォート)
            (r"'yyyy-mm-dd'", 'template_single'),      # 'yyyy-mm-dd' (シングルクォート)
            (r'yyyy-mm-dd(?!["\'])', 'template_none'), # yyyy-mm-dd (クォートなし、後ろにクォートがない)
            (r'\d{4}-\d{2}-\d{2}', 'actual_date')      # 実際の日付 (YYYY-MM-DD)
        ]
        
        # 全ての日付パターンを順序通りに抽出
        found_dates = []
        for pattern, date_type in date_patterns:
            for match in re.finditer(pattern, period_section):
                match_text = match.group()
                match_pos = match.start()
                
                # 重複チェック（同じ位置の日付は除外）
                if not any(abs(match_pos - pos) < 5 for _, _, pos in found_dates):
                    found_dates.append((match_text, date_type, match_pos))
        
        # 位置順でソート（文書内の出現順序）
        found_dates.sort(key=lambda x: x[2])
        
        # 日付が2つない場合はエラー
        if len(found_dates) < 2:
            if len(found_dates) == 0:
                raise ValueError("ログ取得期間に日付が記載されていません。FROM: YYYY-MM-DD TO: YYYY-MM-DD の形式で記載してください。")
            else:
                raise ValueError("ログ取得期間に日付が1つしか記載されていません。FROM: YYYY-MM-DD TO: YYYY-MM-DD の形式で2つの日付を記載してください。")
        
        # 3つ以上ある場合は警告して最初の2つを使用
        if len(found_dates) > 2:
            logger.warning(f"LOG_PERIOD_MULTIPLE_DATES - {len(found_dates)}個の日付が見つかりました。最初の2つを使用します。")
        
        # 1つ目をFROM、2つ目をTOとして処理
        first_date, first_type, _ = found_dates[0]
        second_date, second_type, _ = found_dates[1]
        
        # 混在パターンチェック
        first_is_template = first_type.startswith('template')
        second_is_template = second_type.startswith('template')
        
        if first_is_template != second_is_template:
            raise ValueError("ログ取得期間の日付形式が混在しています。両方とも実際の日付（YYYY-MM-DD）または両方ともテンプレート（\"yyyy-mm-dd\"）で記載してください。")
        
        from_date = ""
        to_date = ""
        
        # FROM日付処理
        if first_is_template:
            # デフォルト値：前日
            yesterday = datetime.now() - timedelta(days=1)
            from_date = yesterday.strftime("%Y-%m-%d")
        else:
            # 実際の日付
            from_date = first_date
        
        # TO日付処理
        if second_is_template:
            # デフォルト値：今日
            today = datetime.now()
            to_date = today.strftime("%Y-%m-%d")
        else:
            # 実際の日付
            to_date = second_date
        
        logger.info(f"LOG_PERIOD_EXTRACTED - FROM:{from_date} TO:{to_date} (Found:{len(found_dates)} dates, Types:{first_type},{second_type})")
        return from_date, to_date

    except ValueError:
        # バリデーションエラーはそのまま再発生
        raise
    except Exception as e:
        logger.warning(f"LOG_PERIOD_EXTRACT_ERROR - {str(e)}")
        raise ValueError(f"ログ取得期間の解析に失敗しました: {str(e)}")

def create_teams_approval_html_message(approval_data: ApprovalData, period_str: str, draft_link: str) -> str:
    """Teams承認用HTMLメッセージ作成"""
    return f"""
<table border="1" style="border-collapse: collapse; width: 100%;">
<tr><td><strong>申請システム</strong></td><td>{approval_data.system}</td></tr>
<tr><td><strong>申請者</strong></td><td>{approval_data.mail}</td></tr>
<tr><td><strong>申請内容</strong></td><td>{approval_data.content.replace('\n', '<br>')}</td></tr>
<tr><td><strong>ログ取得期間</strong></td><td>{period_str}</td></tr>
</table>
<br>
<p><strong>🔗 承認メール作成:</strong></p>
<p><a href="{draft_link}">📧 承認メールを作成する</a></p>
<p><em>※承認する場合は、開いた下書きメールをそのまま送信してください。</em></p>
"""

def create_teams_acceptance_html_message(approval_data: ApprovalData, period_str: str) -> str:
    """Teams受付通知用HTMLメッセージ作成"""
    return f"""
<table border="1" style="border-collapse: collapse; width: 100%;">
<tr><td><strong>申請システム</strong></td><td>{approval_data.system}</td></tr>
<tr><td><strong>申請内容</strong></td><td>{approval_data.content.replace('\n', '<br>')}</td></tr>
<tr><td><strong>ログ取得期間</strong></td><td>{period_str}</td></tr>
</table>
<br>
<p>申請を受け付けました。<br>
承認者による確認後、ログ取得を実行いたします。</p>
"""

def create_correction_request_message(error_message: str, sender_email: str, mail_subject: str) -> str:
    """修正依頼メッセージ作成"""
    return f"""
<table border="1" style="border-collapse: collapse; width: 100%;">
<tr><td><strong>申請システム</strong></td><td>{mail_subject}</td></tr>
<tr><td><strong>エラー内容</strong></td><td>{error_message}</td></tr>
</table>
<br>
<p><strong>修正方法:</strong></p>
<ol>
<li>メール本文に以下の形式で記載してください<br>
【申請理由】<br>
[理由を記載]<br>
【ログ取得期間】<br>
FROM: YYYY-MM-DD<br>
TO: YYYY-MM-DD</li>
<li>修正後、再度メールを送信してください</li>
</ol>
"""

def create_system_error_message(sender_email: str, mail_subject: str) -> str:
    """システムエラーメッセージ作成"""
    return f"""
<table border="1" style="border-collapse: collapse; width: 100%;">
<tr><td><strong>申請システム</strong></td><td>{mail_subject}</td></tr>
<tr><td><strong>エラー</strong></td><td>想定外のエラーが発生しました</td></tr>
</table>
<br>
<p><strong>SD課への依頼をお願いします。</strong><br>
ログ取得APIの処理でシステムエラーが発生しているため、<br>
手動でのログ取得対応をお願いします。</p>
"""

def create_mailto_link(approval_data: ApprovalData) -> str:
    """メール下書きリンク作成"""
    try:
        to = os.environ.get('APPROVAL_SENDER_EMAIL')
        if not to:
            raise APIException(500, "APPROVAL_SENDER_EMAIL環境変数が設定されていません")

        # 改行コード正規化
        def normalize_newlines(value: str) -> str:
            return value.replace('\r\n', '\n').replace('\r', '\n')

        body_json = {
            "mail": approval_data.mail,
            "content": normalize_newlines(approval_data.content),
            "system": approval_data.system,
            "from_date": approval_data.from_date.strftime('%Y-%m-%d'),  # dateを文字列に変換
            "to_date": approval_data.to_date.strftime('%Y-%m-%d'),      # dateを文字列に変換
        }

        body = json.dumps(body_json, ensure_ascii=False)
        subject = f"ログ取得API実行: {approval_data.system}"
        
        subject_enc = urllib.parse.quote(subject)
        body_enc = urllib.parse.quote(body)

        return f"mailto:{to}?subject={subject_enc}&body={body_enc}"

    except Exception as e:
        raise APIException(500, f"メール下書きリンク作成に失敗しました: {str(e)}")

# ========== 4. API呼び出し関数 ==========

def send_teams_approval_message(approval_data: ApprovalData) -> dict:
    """Teams承認メッセージ送信（承認者用）"""
    try:
        # 期間文字列作成（dateオブジェクトを文字列に変換）
        period_str = f"FROM: {approval_data.from_date.strftime('%Y-%m-%d')}"
        if approval_data.to_date:
            period_str += f" TO: {approval_data.to_date.strftime('%Y-%m-%d')}"
        
        # メール下書きリンク作成
        draft_link = create_mailto_link(approval_data)
        
        # HTMLメッセージ作成
        html_message = create_teams_approval_html_message(approval_data, period_str, draft_link)
        
        # Teams APIデータ作成
        teams_data = {
            "mode": 2,
            "team_name": os.environ.get('TEAMS_TEAM_NAME'),
            "channel_name": os.environ.get('TEAMS_CHANNEL_NAME'),
            "message_text": html_message,
            "content_type": "html",
            "subject": "ログ取得の申請：API承認依頼"
        }
        
        # Teams API呼び出し
        result = call_teams_api(teams_data)
        
        logger.info("TEAMS_APPROVAL_MESSAGE_SUCCESS")
        return result
        
    except Exception as e:
        logger.error(f"TEAMS_APPROVAL_MESSAGE_ERROR - {str(e)}")
        raise APIException(502, f"Teams承認メッセージ送信に失敗しました: {str(e)}")

def send_teams_acceptance_notification(approval_data: ApprovalData) -> dict:
    """Teams受付通知送信（申請者用）"""
    try:
        # 期間文字列作成（dateオブジェクトを文字列に変換）
        period_str = f"FROM: {approval_data.from_date.strftime('%Y-%m-%d')}"
        if approval_data.to_date:
            period_str += f" TO: {approval_data.to_date.strftime('%Y-%m-%d')}"
        
        # HTMLメッセージ作成
        html_message = create_teams_acceptance_html_message(approval_data, period_str)
        
        # Teams APIデータ作成（申請者メンション付き）
        teams_data = {
            "mode": 2,
            "team_name": os.environ.get('ERROR_NOTIFICATION_TEAM_NAME'),
            "channel_name": os.environ.get('ERROR_NOTIFICATION_CHANNEL_NAME'),
            "message_text": html_message,
            "content_type": "html",
            "subject": "ログ取得の申請：受付完了",
            "mentions": [
                {
                    "mention_type": "user",
                    "email_address": approval_data.mail
                }
            ]
        }
        
        # Teams API呼び出し
        result = call_teams_api(teams_data)
        
        logger.info("TEAMS_ACCEPTANCE_NOTIFICATION_SUCCESS")
        return result
        
    except Exception as e:
        logger.error(f"TEAMS_ACCEPTANCE_NOTIFICATION_ERROR - {str(e)}")
        raise APIException(502, f"Teams受付通知送信に失敗しました: {str(e)}")

def send_error_notification(error: APIException, sender_email: str, mail_subject: str):
    """エラー通知送信"""
    try:
        if error.status_code >= 400 and error.status_code < 500:
            # 修正可能エラー
            html_message = create_correction_request_message(error.message, sender_email, mail_subject)
            subject = "ログ取得の申請：申請内容の修正が必要です"
        else:
            # 想定外エラー
            html_message = create_system_error_message(sender_email, mail_subject)
            subject = "ログ取得の申請：システムエラーが発生しました"
        
        # Teams APIデータ作成（申請者メンション付き）
        teams_data = {
            "mode": 2,
            "team_name": os.environ.get('ERROR_NOTIFICATION_TEAM_NAME'),
            "channel_name": os.environ.get('ERROR_NOTIFICATION_CHANNEL_NAME'),
            "message_text": html_message,
            "content_type": "html",
            "subject": subject,
            "mentions": [
                {
                    "mention_type": "user",
                    "email_address": sender_email
                }
            ]
        }
        
        # Teams API呼び出し
        result = call_teams_api(teams_data)
        
        logger.info("ERROR_NOTIFICATION_SUCCESS")
        return result
        
    except Exception as e:
        logger.error(f"ERROR_NOTIFICATION_FAILED - {str(e)}")
        return None  # 通知失敗でもメイン処理は継続

def call_teams_api(teams_data: dict) -> dict:
    """Teams API呼び出し"""
    try:
        headers = {"Content-Type": "application/json"}
        request_body = json.dumps(teams_data, ensure_ascii=False).encode("utf-8")
        
        response = http.request("POST", TEAMS_API_URL, headers=headers, body=request_body)
        response_body = response.data.decode() if response.data else ""
        
        # 成功の場合
        if response.status in [200, 201]:
            return json.loads(response_body) if response_body else {}
        
        # エラーの場合：API Gatewayのレスポンスからメッセージを取得
        try:
            response_data = json.loads(response_body)
            api_message = response_data.get('message', 'Unknown error')
            
            error_message = f"Status:{response.status} - {api_message}"
            logger.info(f"TEAMS_API_ERROR - {error_message}")
            raise APIException(502, error_message)
            
        except json.JSONDecodeError:
            raise APIException(502, f"Status:{response.status} - Invalid JSON response")
        
    except APIException:
        raise
    except Exception as e:
        raise APIException(502, f"Teams API通信エラー: {str(e)}")