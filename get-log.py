import boto3
import json
import os
import tempfile
import paramiko
import concurrent.futures
import threading
import uuid
import pyzipper
import urllib3
import logging
import re
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from io import StringIO
import base64
import traceback
from boto3.s3.transfer import TransferConfig
from typing import Optional, List, Dict, Any
import time  # 追加

# ログ設定
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# urllib3設定（タイムアウト付き）
http = urllib3.PoolManager(timeout=urllib3.Timeout(30))

# AWS クライアント設定
ssm = boto3.client('ssm', region_name=os.environ.get('REGION'))
s3 = boto3.client('s3')

# 環境変数
BUCKET_NAME = os.environ.get('BUCKET_NAME')
STORAGE_GATEWAY_ROLE_ARN = os.environ.get('STORAGE_GATEWAY_ROLE_ARN')
FILE_SHARE_ARN = os.environ.get('FILE_SHARE_ARN')
STORAGE_GATEWAY_SHARE_PATH = os.environ.get('STORAGE_GATEWAY_SHARE_PATH')
TEAMS_API_URL = os.environ.get('TEAMS_API_URL')
ERROR_NOTIFICATION_TEAM_NAME = os.environ.get('ERROR_NOTIFICATION_TEAM_NAME')
ERROR_NOTIFICATION_CHANNEL_NAME = os.environ.get('ERROR_NOTIFICATION_CHANNEL_NAME')
INTERNAL_DOMAIN = os.environ.get('INTERNAL_DOMAIN', 'intra.sbilife.co.jp')
SD_TEAM_EMAIL = os.environ.get('SD_TEAM_EMAIL', 'sd-team@example.com')

# S3転送設定
transfer_config = TransferConfig(
    multipart_threshold=1024 * 25,
    max_concurrency=2,
    multipart_chunksize=1024 * 25,
    use_threads=True
)

# ========== 例外クラス ==========

class APIException(Exception):
    """HTTPステータスコードベースの例外クラス"""
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(self.message)

# ========== 1. メインハンドラー ==========

def lambda_handler(event, context):
    """AWS Lambda メインハンドラー関数"""
    approver_email = None
    system = None
    applicant_email = None
    
    try:
        logger.info("REQUEST_START")
        
        validate_environment_variables()
        
        # SESイベント解析
        ses_notification = event['Records'][0]['ses']
        message_id = ses_notification['mail']['messageId']
        approver_email = ses_notification['mail']['source']

        logger.info(f"SES_EVENT_PARSED - {message_id}")

        # メール本文取得・解析
        mail_body = get_email_body_from_s3(message_id)
        body = extract_json_from_email(mail_body)
        system = body['system']
        applicant_email = body['mail']
        from_date = datetime.strptime(body['from_date'], '%Y-%m-%d')
        to_date = datetime.strptime(body['to_date'], '%Y-%m-%d')

        logger.info(f"LOG_PROCESSING_START - {system}")

        # フォルダ名生成
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        folder_name = f"{system}_{timestamp}"

        # ログ処理実行（分割対応）
        config = get_ssm_param(f"/get-log-api/config/{system}")
        storage_paths, password = process_servers_logs(
            config.get("servers", {}), from_date, to_date, folder_name
        )

        # 成功通知（複数パス対応）
        send_success_notifications(body, approver_email, storage_paths, password)

        logger.info("REQUEST_SUCCESS")
        return {"status": "OK"}

    except APIException as e:
        logger.error(f"API_ERROR - Status:{e.status_code} Message:{e.message}")
        send_failure_notification(system, applicant_email, str(e))
        return {"status": "Error", "message": e.message}
    except Exception as e:
        logger.error(f"SYSTEM_ERROR - {str(e)}")
        send_failure_notification(system, applicant_email, str(e))
        return {"status": "Error", "message": str(e)}

# ========== 3. 共通処理関数 ==========

def validate_environment_variables():
    """必要な環境変数の事前チェック"""
    required_vars = {
        'BUCKET_NAME': 'S3バケット名',
        'STORAGE_GATEWAY_ROLE_ARN': 'Storage Gateway AssumeRole ARN',
        'FILE_SHARE_ARN': 'File Share ARN',
        'STORAGE_GATEWAY_SHARE_PATH': 'Storage Gateway共有パス',
        'TEAMS_API_URL': 'Teams API URL',
        'ERROR_NOTIFICATION_TEAM_NAME': 'エラー通知用Teamsチーム名',
        'ERROR_NOTIFICATION_CHANNEL_NAME': 'エラー通知用Teamsチャンネル名'
    }
    
    missing_vars = []
    for var_name, description in required_vars.items():
        if not os.environ.get(var_name):
            missing_vars.append(f"{var_name}({description})")
    
    if missing_vars:
        logger.error(f"ENV_VALIDATION_ERROR - Missing: {', '.join(missing_vars)}")
        raise APIException(500, f"必要な環境変数が設定されていません: {', '.join(missing_vars)}")
    
    logger.info("ENV_VALIDATION_SUCCESS")

def get_email_body_from_s3(message_id: str) -> str:
    """S3からメール本文取得"""
    try:
        logger.info(f"S3_GET_EMAIL - {message_id}")
        
        response = s3.get_object(Bucket=BUCKET_NAME, Key=f"send/{message_id}")
        raw_email = response['Body'].read()
        msg = BytesParser(policy=policy.default).parsebytes(raw_email)

        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    charset = part.get_content_charset() or 'utf-8'
                    return part.get_payload(decode=True).decode(charset)
        else:
            charset = msg.get_content_charset() or 'utf-8'
            return msg.get_payload(decode=True).decode(charset)

    except Exception as e:
        logger.error(f"S3_EMAIL_ERROR - {str(e)}")
        raise APIException(500, f"S3からのメール取得に失敗しました: {str(e)}")

def extract_json_from_email(mail_body: str) -> dict:
    """メール本文からJSON部分を抽出"""
    try:
        # 最初の{から最後の}までを抽出
        start_idx = mail_body.find('{')
        if start_idx == -1:
            raise ValueError("JSON形式のデータが見つかりません")
        
        # 対応する}を見つける
        brace_count = 0
        end_idx = -1
        
        for i in range(start_idx, len(mail_body)):
            if mail_body[i] == '{':
                brace_count += 1
            elif mail_body[i] == '}':
                brace_count -= 1
                if brace_count == 0:
                    end_idx = i
                    break
        
        if end_idx == -1:
            raise ValueError("JSON形式が不正です（閉じ括弧が見つかりません）")
        
        json_str = mail_body[start_idx:end_idx + 1]
        
        # JSONパース
        return json.loads(json_str)
            
    except Exception as e:
        logger.error(f"JSON_EXTRACT_ERROR - {str(e)}")
        raise APIException(400, f"メール本文からのJSON抽出に失敗しました: {str(e)}")

def get_ssm_param(name: str) -> dict:
    """SSMパラメータ取得"""
    try:
        logger.info(f"SSM_GET_PARAM - {name}")
        param_value = ssm.get_parameter(Name=name.strip(), WithDecryption=True)['Parameter']['Value']
        return json.loads(param_value)
    except Exception as e:
        logger.error(f"SSM_GET_PARAM_ERROR - {str(e)}")
        raise APIException(500, f"SSMパラメータの取得に失敗しました: {str(e)}")

def get_credentials_from_ssm(hostname: str) -> dict:
    """SSM認証情報取得"""
    try:
        secret_name = f"/get-log-api/credentials/{hostname}"
        logger.info(f"SSM_GET_CREDENTIALS - {hostname}")
        secret = ssm.get_parameter(Name=secret_name.strip(), WithDecryption=True)
        return json.loads(secret['Parameter']['Value'])
    except Exception as e:
        logger.error(f"SSM_GET_CREDENTIALS_ERROR - {str(e)}")
        raise APIException(500, f"認証情報の取得に失敗しました: {str(e)}")

def get_ssh_auth(credentials: dict) -> tuple[str, dict]:
    """SSH認証パラメータ生成（PEMファイル完全対応）"""
    username = credentials.get('username')
    
    if 'password' in credentials:
        return username, {'password': credentials['password']}
    elif 'client_cert' in credentials:
        client_cert_b64 = credentials['client_cert']
        private_key_str = base64.b64decode(client_cert_b64).decode('utf-8')
        private_key_file = StringIO(private_key_str)
        
        # PEMファイルの鍵タイプを自動判定
        try:
            # RSA鍵を試行
            private_key = paramiko.RSAKey.from_private_key(private_key_file)
            logger.info("SSH_KEY_TYPE - RSA")
        except paramiko.ssh_exception.SSHException:
            try:
                private_key_file.seek(0)  # ファイルポインタをリセット
                # ED25519鍵を試行
                private_key = paramiko.Ed25519Key.from_private_key(private_key_file)
                logger.info("SSH_KEY_TYPE - ED25519")
            except paramiko.ssh_exception.SSHException:
                try:
                    private_key_file.seek(0)
                    # ECDSA鍵を試行
                    private_key = paramiko.ECDSAKey.from_private_key(private_key_file)
                    logger.info("SSH_KEY_TYPE - ECDSA")
                except paramiko.ssh_exception.SSHException:
                    try:
                        private_key_file.seek(0)
                        # DSA鍵を試行
                        private_key = paramiko.DSSKey.from_private_key(private_key_file)
                        logger.info("SSH_KEY_TYPE - DSA")
                    except paramiko.ssh_exception.SSHException:
                        logger.error("SSH_KEY_TYPE - UNSUPPORTED")
                        raise ValueError("サポートされていない鍵形式です")
        
        return username, {'pkey': private_key}
    else:
        raise ValueError("サポートされていない認証形式です")

def process_servers_logs(servers: dict, from_date: datetime, to_date: datetime, folder_name: str) -> tuple[List[str], str]:
    """全サーバーログ処理（分割対応）"""
    all_downloaded_files = []
    lambda_storage_limit = 8 * 1024 * 1024 * 1024  # 8GB（10GBの80%）
    current_storage_usage = 0
    zip_files = []  # 作成されたZIPファイルのパスリスト
    password = str(uuid.uuid4()).replace('-', '')[:10]  # 共通パスワード
    part_number = 1
    
    try:
        for hostname, server_info in servers.items():
            try:
                downloaded_files, storage_used = process_single_server(
                    hostname, server_info, from_date, to_date
                )
                
                # ストレージ容量チェック
                if current_storage_usage + storage_used > lambda_storage_limit:
                    logger.warning(f"LAMBDA_STORAGE_LIMIT_APPROACHING - Creating part {part_number}")
                    
                    # 現在のファイルでZIP作成・アップロード
                    if all_downloaded_files:
                        zip_path = create_part_zip(all_downloaded_files, folder_name, part_number, password)
                        storage_path = upload_zip_to_storage_gateway(zip_path, f"{folder_name}_part{part_number}")
                        zip_files.append(storage_path)
                        part_number += 1
                        
                        # ストレージクリーンアップ
                        cleanup_temp_files(all_downloaded_files)
                        os.remove(zip_path)
                        all_downloaded_files = []
                        current_storage_usage = get_actual_tmp_usage()
                
                # 新しいファイルを追加
                all_downloaded_files.extend(downloaded_files)
                current_storage_usage += storage_used
                logger.info(f"SERVER_PROCESSING_COMPLETE - {hostname}")
                
            except Exception as e:
                logger.error(f"SERVER_PROCESSING_ERROR - {hostname}: {str(e)}")
                continue

        # 残りのファイルで最終ZIP作成
        if all_downloaded_files:
            if zip_files:  # 既に分割ZIPがある場合
                zip_path = create_part_zip(all_downloaded_files, folder_name, part_number, password)
                storage_path = upload_zip_to_storage_gateway(zip_path, f"{folder_name}_part{part_number}")
                zip_files.append(storage_path)
                os.remove(zip_path)
            else:  # 分割不要の場合
                zip_path = create_single_zip(all_downloaded_files, folder_name, password)
                storage_path = upload_zip_to_storage_gateway(zip_path, folder_name)
                zip_files.append(storage_path)
                os.remove(zip_path)
            
            cleanup_temp_files(all_downloaded_files)
        
        return zip_files, password

    except Exception as e:
        cleanup_temp_files(all_downloaded_files)
        raise

def process_single_server(hostname: str, server_info: dict, from_date: datetime, to_date: datetime) -> tuple[List[dict], int]:
    """単一サーバーログ処理"""
    port = server_info.get('port', 22)
    log_paths = server_info.get('log_paths', [])

    credentials = get_credentials_from_ssm(hostname)
    username, ssh_auth = get_ssh_auth(credentials)
    fqdn_hostname = f"{hostname}.{INTERNAL_DOMAIN}"

    expanded_paths = expand_log_paths(log_paths, from_date, to_date)
    return download_logs_from_server(fqdn_hostname, port, username, ssh_auth, expanded_paths)

def expand_log_paths(log_paths: List[str], from_date: datetime, to_date: datetime) -> List[str]:
    """ログパス展開"""
    date_patterns = {'yyyy-mm-dd': '%Y-%m-%d', 'yyyymmdd': '%Y%m%d'}
    expanded_paths = []
    
    for path in log_paths:
        matched = False
        for pattern, fmt in date_patterns.items():
            if pattern in path:
                current = from_date
                while current <= to_date:
                    expanded_paths.append(path.replace(pattern, current.strftime(fmt)))
                    current += timedelta(days=1)
                matched = True
                break
        if not matched:
            expanded_paths.append(path)
    
    return expanded_paths

def download_logs_from_server(hostname: str, port: int, username: str, ssh_auth: dict, log_paths: List[str]) -> tuple[List[dict], int]:
    """サーバーからログダウンロード（リトライ対応）"""
    downloaded_files = []
    total_storage_used = 0
    max_retries = 3
    retry_delay = 5  # 秒
    
    for attempt in range(max_retries):
        try:
            with paramiko.SSHClient() as ssh:
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                
                if 'password' in ssh_auth:
                    ssh.connect(hostname=hostname, port=port, username=username, 
                              password=ssh_auth['password'], timeout=30)
                elif 'pkey' in ssh_auth:
                    ssh.connect(hostname=hostname, port=port, username=username, 
                              pkey=ssh_auth['pkey'], timeout=30)
                
                logger.info(f"SSH_CONNECTION_SUCCESS - {hostname} (Attempt {attempt + 1})")
                
                max_workers = max(1, min(2, len(log_paths)))
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(download_single_file_with_retry, ssh, hostname, path): path
                        for path in log_paths
                    }
                    
                    for future in concurrent.futures.as_completed(futures):
                        path = futures[future]
                        try:
                            file_info = future.result()
                            downloaded_files.append(file_info)
                            total_storage_used += file_info['file_size']
                            logger.info(f"FILE_DOWNLOAD_SUCCESS - {path}")
                        except Exception as e:
                            logger.error(f"FILE_DOWNLOAD_ERROR - {path}: {str(e)}")
                            continue
                
                # 成功した場合はループを抜ける
                return downloaded_files, total_storage_used
                
        except Exception as e:
            logger.warning(f"SSH_CONNECTION_ERROR - {hostname} (Attempt {attempt + 1}/{max_retries}): {str(e)}")
            
            if attempt < max_retries - 1:
                logger.info(f"SSH_RETRY_WAIT - {hostname} - Waiting {retry_delay} seconds")
                time.sleep(retry_delay)
                retry_delay *= 2  # 指数バックオフ
            else:
                logger.error(f"SSH_CONNECTION_FAILED - {hostname} - All {max_retries} attempts failed")
                raise APIException(500, f"SSH接続に失敗しました ({max_retries}回試行): {str(e)}")
    
    return downloaded_files, total_storage_used

def download_single_file_with_retry(ssh, hostname: str, path: str) -> dict:
    """単一ファイルダウンロード（リトライ対応）"""
    max_retries = 3
    retry_delay = 2  # 秒
    
    for attempt in range(max_retries):
        unique_id = str(uuid.uuid4())[:8]
        filename = os.path.basename(path)
        tmp_filename = f"/tmp/{unique_id}_{filename}"
        
        try:
            with ssh.open_sftp() as sftp:
                sftp.get(path, tmp_filename)
                file_size = os.path.getsize(tmp_filename)
                
                return {
                    'original_path': path,
                    'local_path': tmp_filename,
                    'relative_path': f"{hostname.replace(f'.{INTERNAL_DOMAIN}', '')}/{path.lstrip('/')}",
                    'file_size': file_size
                }
                
        except Exception as e:
            if os.path.exists(tmp_filename):
                os.remove(tmp_filename)
            
            logger.warning(f"FILE_DOWNLOAD_RETRY - {path} (Attempt {attempt + 1}/{max_retries}): {str(e)}")
            
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                retry_delay *= 1.5  # 軽い指数バックオフ
            else:
                logger.error(f"FILE_DOWNLOAD_FAILED - {path} - All {max_retries} attempts failed")
                raise APIException(500, f"ファイルダウンロードに失敗しました ({max_retries}回試行): {str(e)}")
    
    # この行には到達しないはずだが、型チェック用
    raise APIException(500, "予期しないエラー")

def create_part_zip(downloaded_files: List[dict], folder_name: str, part_number: int, password: str) -> str:
    """分割ZIP作成"""
    try:
        zip_name = f"{folder_name}_part{part_number}"
        zip_path = f"/tmp/{zip_name}.zip"
        
        logger.info(f"PART_ZIP_CREATION_START - Part:{part_number} Files:{len(downloaded_files)}")
        
        with pyzipper.AESZipFile(zip_path, 'w', compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as zf:
            zf.setpassword(password.encode('utf-8'))
            for file_info in downloaded_files:
                zf.write(file_info['local_path'], file_info['relative_path'])
        
        zip_size = os.path.getsize(zip_path)
        logger.info(f"PART_ZIP_CREATION_SUCCESS - Part:{part_number} Size:{zip_size/1024/1024:.1f}MB")
        
        return zip_path
        
    except Exception as e:
        logger.error(f"PART_ZIP_CREATION_ERROR - Part:{part_number}: {str(e)}")
        raise APIException(500, f"分割ZIP作成に失敗しました (Part {part_number}): {str(e)}")

def create_single_zip(downloaded_files: List[dict], folder_name: str, password: str) -> str:
    """単一ZIP作成"""
    try:
        zip_path = f"/tmp/{folder_name}.zip"
        
        logger.info(f"SINGLE_ZIP_CREATION_START - Files:{len(downloaded_files)}")
        
        with pyzipper.AESZipFile(zip_path, 'w', compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as zf:
            zf.setpassword(password.encode('utf-8'))
            for file_info in downloaded_files:
                zf.write(file_info['local_path'], file_info['relative_path'])
        
        zip_size = os.path.getsize(zip_path)
        logger.info(f"SINGLE_ZIP_CREATION_SUCCESS - Size:{zip_size/1024/1024:.1f}MB")
        
        return zip_path
        
    except Exception as e:
        logger.error(f"SINGLE_ZIP_CREATION_ERROR - {str(e)}")
        raise APIException(500, f"ZIP作成に失敗しました: {str(e)}")

def upload_zip_to_storage_gateway(zip_file_path: str, zip_name: str) -> str:
    """ZIPファイルをStorage Gatewayにアップロード"""
    try:
        s3_key = f"logs/{zip_name}.zip"
        logger.info(f"S3_UPLOAD_START - {s3_key}")
        
        s3.upload_file(zip_file_path, BUCKET_NAME, s3_key, Config=transfer_config)
        
        logger.info(f"S3_UPLOAD_SUCCESS - {s3_key}")
        
        # キャッシュ更新（一時的にコメントアウト）
        # trigger_cache_refresh(s3_key, zip_name)
        
        storage_path = f"{STORAGE_GATEWAY_SHARE_PATH}\\{zip_name}.zip"
        logger.info(f"STORAGE_PATH_GENERATED - {storage_path}")
        
        return storage_path
        
    except Exception as e:
        logger.error(f"ZIP_UPLOAD_ERROR - {str(e)}")
        raise APIException(500, f"ZIPファイルのアップロードに失敗しました: {str(e)}")

# キャッシュ更新関数（一時的にコメントアウト）
# def trigger_cache_refresh(s3_key: str, folder_name: str) -> str:
#     """キャッシュ更新"""
#     try:
#         # AssumeRole部分をコメントアウト
#         # sts_client = boto3.client('sts')
#         # assumed_role = sts_client.assume_role(
#         #     RoleArn=STORAGE_GATEWAY_ROLE_ARN,
#         #     RoleSessionName=f"LogProcessing-{folder_name}"
#         # )
#         
#         # credentials = assumed_role['Credentials']
#         
#         # storage_gateway_client = boto3.client(
#         #     'storagegateway',
#         #     aws_access_key_id=credentials['AccessKeyId'],
#         #     aws_secret_access_key=credentials['SecretAccessKey'],
#         #     aws_session_token=credentials['SessionToken'],
#         #     region_name=os.environ.get('REGION', 'ap-northeast-1')
#         # )
#         
#         # logger.info(f"CACHE_REFRESH_START - {s3_key}")
#         
#         # storage_gateway_client.refresh_cache(
#         #     FileShareARN=FILE_SHARE_ARN,
#         #     FolderList=[f"logs/{folder_name}.zip"]
#         # )
#         
#         storage_path = f"{STORAGE_GATEWAY_SHARE_PATH}\\{folder_name}.zip"
#         
#         logger.info(f"CACHE_REFRESH_SUCCESS - {storage_path}")
#         return storage_path
#         
#     except Exception as e:
#         logger.error(f"CACHE_REFRESH_ERROR - {str(e)}")
#         raise APIException(500, f"キャッシュ更新に失敗しました: {str(e)}")

def get_actual_tmp_usage() -> int:
    """実際の/tmp使用量を取得"""
    total_size = 0
    try:
        for root, dirs, files in os.walk('/tmp'):
            for file in files:
                file_path = os.path.join(root, file)
                if os.path.exists(file_path):
                    total_size += os.path.getsize(file_path)
    except Exception as e:
        logger.warning(f"TMP_USAGE_CHECK_ERROR - {str(e)}")
    return total_size

def cleanup_temp_files(downloaded_files: List[dict]):
    """一時ファイルクリーンアップ"""
    for file_info in downloaded_files:
        local_path = file_info.get('local_path')
        if local_path and os.path.exists(local_path):
            try:
                os.remove(local_path)
            except Exception as e:
                logger.warning(f"CLEANUP_ERROR - {local_path}: {str(e)}")

# ========== 4. 通知関数 ==========

def send_success_notifications(request_info: dict, approver_email: str, storage_paths: List[str], password: str):
    """成功通知送信（複数パス対応）"""
    try:
        send_applicant_dm(request_info['mail'], storage_paths, password, request_info)
        send_channel_notification(request_info, approver_email)
        logger.info("SUCCESS_NOTIFICATIONS_SENT")
    except Exception as e:
        logger.error(f"SUCCESS_NOTIFICATION_ERROR - {str(e)}")
        raise APIException(502, f"通知送信に失敗しました: {str(e)}")

def send_applicant_dm(applicant_email: str, storage_paths: List[str], password: str, request_info: dict):
    """申請者DM送信（複数パス対応）"""
    try:
        # ファイルパス部分を動的生成
        if len(storage_paths) == 1:
            file_paths_html = f"<tr><td><strong>ファイルパス</strong></td><td>{storage_paths[0]}</td></tr>"
        else:
            paths_list = "<br>".join([f"Part {i+1}: {path}" for i, path in enumerate(storage_paths)])
            file_paths_html = f"<tr><td><strong>ファイルパス<br>（分割ファイル）</strong></td><td>{paths_list}</td></tr>"
        
        message_html = f"""
<p><strong>ログ取得が完了しました</strong></p>
<table border="1" style="border-collapse: collapse; width: 100%;">
<tr><td><strong>申請システム</strong></td><td>{request_info['system']}</td></tr>
<tr><td><strong>取得期間</strong></td><td>{request_info['from_date']} ～ {request_info['to_date']}</td></tr>
{file_paths_html}
<tr><td><strong>パスワード</strong></td><td>{password}</td></tr>
</table>
<br>
<p><strong>アクセス方法:</strong></p>
<ol>
<li>上記のファイルパスにアクセスしてください</li>
<li>ZIPファイルを開く際に上記のパスワードを入力してください</li>
{"<li><strong>※ 分割ファイルの場合、すべてのファイルをダウンロードしてください</strong></li>" if len(storage_paths) > 1 else ""}
</ol>
"""
        
        teams_data = {
            "mode": 1,
            "email_addresses": [applicant_email],
            "message_text": message_html,
            "content_type": "html",
            "mentions": []
        }
        
        call_teams_api(teams_data)
        logger.info(f"APPLICANT_DM_SENT - {applicant_email} - Files:{len(storage_paths)}")
        
    except Exception as e:
        logger.error(f"APPLICANT_DM_ERROR - {str(e)}")
        raise

def send_channel_notification(request_info: dict, approver_email: str):
    """チャンネル通知送信"""
    try:
        message_html = f"""
<p><strong>ログ取得申請が完了しました</strong></p>
<table border="1" style="border-collapse: collapse; width: 100%;">
<tr><td><strong>申請者</strong></td><td>{request_info['mail']}</td></tr>
<tr><td><strong>申請理由</strong></td><td>{request_info['content']}</td></tr>
<tr><td><strong>対象システム</strong></td><td>{request_info['system']}</td></tr>
<tr><td><strong>取得期間</strong></td><td>{request_info['from_date']} ～ {request_info['to_date']}</td></tr>
<tr><td><strong>承認者</strong></td><td>{approver_email}</td></tr>
</table>
"""
        
        teams_data = {
            "mode": 2,
            "team_name": ERROR_NOTIFICATION_TEAM_NAME,
            "channel_name": ERROR_NOTIFICATION_CHANNEL_NAME,
            "message_text": message_html,
            "content_type": "html",
            "subject": "ログ取得申請完了通知",
            "mentions": []
        }
        
        call_teams_api(teams_data)
        logger.info("CHANNEL_NOTIFICATION_SENT")
        
    except Exception as e:
        logger.error(f"CHANNEL_NOTIFICATION_ERROR - {str(e)}")
        raise

def send_failure_notification(system: str, applicant_email: str, error_message: str):
    """失敗通知送信"""
    try:
        message_html = f"""
<table border="1" style="border-collapse: collapse; width: 100%;">
<tr><td><strong>申請システム</strong></td><td>{system or '不明'}</td></tr>
<tr><td><strong>申請者</strong></td><td>{applicant_email or '不明'}</td></tr>
<tr><td><strong>エラー</strong></td><td>ログ取得処理でエラーが発生しました</td></tr>
</table>
<br>
<p><strong>SD課への依頼をお願いします。</strong><br>
ログ取得APIの処理でシステムエラーが発生しているため、<br>
手動でのログ取得対応をお願いします。</p>
<br>
<p><strong>エラー詳細:</strong><br>
{error_message}</p>
"""
        
        teams_data = {
            "mode": 2,
            "team_name": ERROR_NOTIFICATION_TEAM_NAME,
            "channel_name": ERROR_NOTIFICATION_CHANNEL_NAME,
            "message_text": message_html,
            "content_type": "html",
            "subject": "ログ取得処理失敗通知",
            "mentions": [
                {
                    "mention_type": "user",
                    "email_address": SD_TEAM_EMAIL
                }
            ]
        }
        
        call_teams_api(teams_data)
        logger.info("FAILURE_NOTIFICATION_SENT")
        
    except Exception as e:
        logger.error(f"FAILURE_NOTIFICATION_ERROR - {str(e)}")

def call_teams_api(teams_data: dict) -> dict:
    """Teams API呼び出し"""
    try:
        headers = {"Content-Type": "application/json"}
        request_body = json.dumps(teams_data, ensure_ascii=False).encode("utf-8")
        
        response = http.request("POST", TEAMS_API_URL, headers=headers, body=request_body)
        response_body = response.data.decode() if response.data else ""
        
        if response.status in [200, 201]:
            return json.loads(response_body) if response_body else {}
        
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