import boto3
import json
import os
import tempfile
import paramiko
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from io import BytesIO
import traceback

# AWS クライアント初期化
ssm = boto3.client('ssm')
secretsmanager = boto3.client('secretsmanager')
s3 = boto3.client('s3')
ses = boto3.client('ses', region_name='us-east-1')

# 環境変数の取得
BUCKET_NAME = os.environ.get('BUCKET_NAME')
SES_SOURCE_EMAIL = os.environ.get('SENDER_EMAIL')
SES_NOTIFY_EMAIL = os.environ.get('APPROVAL_RECEIVE_EMAIL')
LOG_RECEIVE_EMAIL = os.environ.get('LOG_RECEIVE_EMAIL')

def lambda_handler(event, context):
    try:
        
        # SES通知からメッセージID取得（S3のオブジェクトキーに利用）
        ses_notification = event['Records'][0]['ses']
        message_id = ses_notification['mail']['messageId']
        print(f"[INFO] Received message ID: {message_id}")
        # S3からメール本文を取得・パース
        mail_body = get_email_body_from_s3(message_id)
        if isinstance(mail_body, dict) and mail_body.get('statusCode') == 300:
            return mail_body  # エラー応答
        print("[INFO] Mail body parsed successfully.")

        # JSON本文をパース（ログ取得条件）
        body = json.loads(mail_body)
        system = body['system']
        from_date = datetime.strptime(body['from_date'], '%Y-%m-%d')
        to_date = datetime.strptime(body['to_date'], '%Y-%m-%d')
        print(f"[INFO] Request body parsed successfully. System: {system}, From: {from_date}, To: {to_date}")

        # SSMパラメータから接続先情報を取得
        hostname = get_ssm_param(f"get-log-api/{system}/hostname")
        port = int(get_ssm_param(f"get-log-api/{system}/port"))
        log_paths = json.loads(get_ssm_param(f"get-log-api/{system}/log-paths"))
        print(f"[INFO] SSM parameters retrieved successfully. Host: {hostname}, Port: {port}, Log paths: {log_paths}")

        # Secrets ManagerからSSH認証情報を取得
        credentials = get_credentials_from_secrets_manager(hostname)
        username = credentials['username']
        password = credentials['password']
        print(f"[INFO] SSH credentials retrieved successfully. Username: {username}")

        # ログパスを日付範囲に展開（パターン置換）
        expanded_paths = expand_log_paths(log_paths, from_date, to_date)
        print(f"[INFO] Log paths expanded successfully. Expanded paths: {expanded_paths}")
        # SFTPでログ取得・S3にアップロード・署名付きURL生成
        uploaded_files = upload_logs_to_s3(hostname, port, username, password, expanded_paths)
        print(f"[INFO] Logs uploaded successfully. Uploaded files: {uploaded_files}")
        try:
            # Teams通知用のSESメール送信（ログリンク/申請情報）
            send_teams_notification_log_link(uploaded_files)
            send_teams_notification_request_info(body)
        except Exception as e:
            print(f"[WARN] Teams notification failed: {e}")
            traceback.print_exc()
        return {"status": "OK"}
        print("[INFO] Lambda function completed successfully.")

    except Exception as e:
        print(f"[ERROR] Unhandled error: {e}")
        traceback.print_exc()
        return {"status": "Error", "message": str(e)}

def get_email_body_from_s3(message_id):
    try:
        # S3から元メールデータを取得
        response = s3.get_object(Bucket=BUCKET_NAME, Key=f"send/{message_id}")
        raw_email = response['Body'].read()

        # MIME形式のメールをパース
        msg = BytesParser(policy=policy.default).parsebytes(raw_email)

        mail_body = ""
        if msg.is_multipart():
            # multipart の場合、text/plain パートを探す
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    charset = part.get_content_charset() or 'utf-8'
                    mail_body = part.get_payload(decode=True).decode(charset)
                    break
        else:
            # 単一パート
            charset = msg.get_content_charset() or 'utf-8'
            mail_body = msg.get_payload(decode=True).decode(charset)

        print("[INFO] メール本文:\n", mail_body)
        return mail_body
    except Exception as e:
        print(f"[ERROR] Failed to retrieve email body: {e}")
        traceback.print_exc()
        return {'statusCode': 300, 'body': json.dumps(f'Failed to retrieve email body from S3: {str(e)}')}

def get_ssm_param(name):
    try:
        return ssm.get_parameter(Name=name, WithDecryption=True)['Parameter']['Value']
    except Exception as e:
        print(f"[ERROR] Failed to get SSM parameter '{name}': {e}")
        raise

def get_credentials_from_secrets_manager(hostname):
    # ホスト名ベースでSecretを構築
    secret_name = f"{hostname}-ssh-password"
    try:
        secret = secretsmanager.get_secret_value(SecretId=secret_name)
        return json.loads(secret['SecretString'])
    except Exception as e:
        print(f"[ERROR] Failed to get secret '{secret_name}': {e}")
        raise

def expand_log_paths(log_paths, from_date, to_date):
    expanded = []
    for path in log_paths:
        # 日付部分プレースホルダーが含まれていれば、範囲展開
        if 'yyyy-mm-dd' in path:
            current = from_date
            while current <= to_date:
                expanded.append(path.replace('yyyy-mm-dd', current.strftime('%Y-%m-%d')))
                current += timedelta(days=1)
        else:
            expanded.append(path)
    return expanded

def upload_logs_to_s3(hostname, port, username, password, log_paths):
    uploaded_files = []
    today_prefix = datetime.now().strftime('logs/%Y%m%d/')
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    sftp = None

    try:
        # SSH接続開始
        ssh.connect(hostname, port=port, username=username, password=password)
        sftp = ssh.open_sftp()

        for path in log_paths:
            try:
                # ログファイルをメモリに取得
                file_data = BytesIO()
                sftp.getfo(path, file_data)
                file_data.seek(0)

                # S3オブジェクトキー生成
                base_name = os.path.basename(path).replace("/", "_")
                s3_key = f"{today_prefix}{base_name}"

                # S3にアップロード
                s3.put_object(Bucket=BUCKET_NAME, Key=s3_key, Body=file_data.read())

                # 署名付きURLを生成
                presigned_url = s3.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': BUCKET_NAME, 'Key': s3_key},
                    ExpiresIn=3600
                )

                uploaded_files.append({
                    'filename': base_name,
                    's3_key': s3_key,
                    'url': presigned_url
                })

                print(f"[INFO] Uploaded: {s3_key}")
                print(f"[INFO] URL: {presigned_url}")

            except Exception as e:
                print(f"[WARN] Failed to get/upload log file '{path}': {e}")
                traceback.print_exc()
    except Exception as e:
        print(f"[ERROR] SSH connection failed to {hostname}:{port} - {e}")
        traceback.print_exc()
        raise
    finally:
        # SFTP・SSHクローズ処理（あれば）
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass
        ssh.close()

    return uploaded_files

def send_teams_notification_log_link(uploaded_files):
    subject = "【ログ取得完了】ログファイルリンク通知"
    message_lines = [
        "ログファイルの取得が完了しました。",
        "以下のリンクをクリックし、ダウンロードしてください。"
    ]
    for file_info in uploaded_files:
        message_lines.append(f"{file_info['filename']}（{file_info['url']}）")

    send_email_via_ses(
        to_addresses=[LOG_RECEIVE_EMAIL],
        subject=subject,
        body_text="\n".join(message_lines),
    )

def send_teams_notification_request_info(info):
    subject = "【ログ取得申請】申請情報通知"
    body = (
        f"ログ取得申請がありました。\n"
        f"申請者: {info.get('mail')}\n"
        f"理由: {info.get('content')}\n"
        f"対象システム: {info.get('system')}\n"
        f"期間: {info.get('from_date')} ～ {info.get('to_date')}\n"
        f"承認者: {info.get('approver', '未設定')}"
    )
    send_email_via_ses(
        to_addresses=[SES_NOTIFY_EMAIL],
        subject=subject,
        body_text=body,
    )

def send_email_via_ses(to_addresses, subject, body_text):
    try:
        response = ses.send_email(
            Source=SES_SOURCE_EMAIL,
            Destination={'ToAddresses': to_addresses},
            Message={
                'Subject': {'Data': subject, 'Charset': 'UTF-8'},
                'Body': {'Text': {'Data': body_text, 'Charset': 'UTF-8'}}
            }
        )
        print(f"[INFO] SES email sent: MessageId={response['MessageId']}")
    except Exception as e:
        print(f"[ERROR] Failed to send SES email: {e}")
        traceback.print_exc()
