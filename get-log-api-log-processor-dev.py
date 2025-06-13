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
import socket
import base64

print("resolved ssm endpoint:", socket.gethostbyname("ssm.ap-northeast-1.amazonaws.com"))

# AWS クライアント初期化
ssm = boto3.client('ssm')
secretsmanager = boto3.client('secretsmanager')
s3 = boto3.client('s3')
ses = boto3.client('ses')

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
        sender_email = ses_notification['mail']['source']
        print(f"sender_email: {sender_email}")
        # S3からメール本文を取得・パース
        mail_body = get_email_body_from_s3(message_id)
        if isinstance(mail_body, dict) and mail_body.get('statusCode') == 300:
            raise mail_body
        print(f"mail_body: {mail_body}")

        # JSON本文をパース（ログ取得条件）
        body = json.loads(mail_body)
        body['approver'] = sender_email
        system, from_date, to_date, mail, content, approver = check_mail_body(body)
        print(f"system: {system}")
        print(f"from_date: {from_date}")
        print(f"to_date: {to_date}")
        print(f"mail: {mail}")
        print(f"content: {content}")
        print(f"approver: {approver}")

        # SSMパラメータから接続先情報を取得
        print("[DEBUG] about to call SSM")
        hostname = get_ssm_param(f"/get-log-api/{system}/hostname")
        print(f"[DEBUG] got SSM hostname: {hostname}")
        port = int(get_ssm_param(f"/get-log-api/{system}/port"))
        log_paths = json.loads(get_ssm_param(f"/get-log-api/{system}/log-paths"))
        print(f"log_paths: {log_paths}")

        # Secrets ManagerからSSH認証情報を取得
        credentials = get_credentials_from_secrets_manager(hostname)
        username = credentials['username']
        password = credentials['password']
        client_certificate = credentials.get('client-certificate')
        print(f"Successfully got credentials")

        # ログパスを日付範囲に展開（パターン置換）
        expanded_paths = expand_log_paths(log_paths, from_date, to_date)
        print(f"Successfully expanded log paths")
        # SFTPでログ取得・S3にアップロード・署名付きURL生成
        uploaded_files = upload_logs_to_s3(hostname, port, username, password, client_certificate, expanded_paths)
        print(f"Successfully uploaded logs to S3")
        try:
            # Teams通知用のSESメール送信（ログリンク/申請情報）
            send_teams_notification_log_link(uploaded_files)
            send_teams_notification_request_info(body)
            print(f"Successfully sent teams notification")
        except Exception as e:
            print(f"[WARN] Teams notification failed: {e}")
            traceback.print_exc()

        return {"status": "OK"}

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
            if to_date is None:
                # to_dateがNoneの場合はfrom_dateの日付のみを使用
                expanded.append(path.replace('yyyy-mm-dd', from_date.strftime('%Y-%m-%d')))
            else:
                # 日付範囲がある場合は範囲展開
                while from_date <= to_date:
                    expanded.append(path.replace('yyyy-mm-dd', from_date.strftime('%Y-%m-%d')))
                    from_date += timedelta(days=1)
        else:
            expanded.append(path)
    return expanded

def upload_logs_to_s3(hostname, port, username, password, client_certificate, log_paths):
    uploaded_files = []
    today_prefix = datetime.now().strftime('logs/%Y%m%d/')
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    sftp = None

    try:
        # SSH接続開始
        if client_certificate:
            try:
                # base64デコードを試みる
                decoded_certificate = base64.b64decode(client_certificate)
                
                # 一時ファイルにPEM形式で書き出し
                with tempfile.NamedTemporaryFile(mode='wb', delete=False) as temp_file:
                    temp_file.write(decoded_certificate)
                    temp_file.flush()
                    temp_file_path = temp_file.name

                try:
                    # PEMファイルから秘密鍵を読み込む
                    key = paramiko.RSAKey.from_private_key_file(temp_file_path)
                    ssh.connect(hostname, port=port, username=username, pkey=key)
                finally:
                    # 一時ファイルを削除
                    os.unlink(temp_file_path)
                    
            except Exception as e:
                print(f"[ERROR] Failed to decode or use client certificate: {e}")
                # 証明書の処理に失敗した場合はパスワード認証にフォールバック
                print("[INFO] Falling back to password authentication")
                ssh.connect(hostname, port=port, username=username, password=password)
        else:
            # パスワード認証を使用
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
    subject = "（ログ取得完了）ログファイルリンク通知"
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
    subject = "（ログ取得申請）申請情報通知"
    body = (
        f"ログ取得申請がありました。\n"
        f"申請者: {info.get('mail')}\n"
        f"理由: {info.get('content')}\n"
        f"対象システム: {info.get('system')}\n"
        f"期間: {info.get('from_date')} 〜 {info.get('to_date')}\n"
        f"承認者: {info.get('approver', '未設定') }"
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

def is_valid_date(date_str):
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
        return True
    except (ValueError, TypeError):
        return False

def check_mail_body(body):
    # 必須項目の定義とバリデーションルール
    validation_rules = {
        'system': {'name': '対象システム', 'is_date': False, 'required': True},
        'from_date': {'name': '開始日', 'is_date': True, 'required': True},
        'to_date': {'name': '終了日', 'is_date': True, 'required': False},
        'mail': {'name': '申請者メールアドレス', 'is_date': False, 'required': True},
        'content': {'name': '申請理由', 'is_date': False, 'required': True},
        'approver': {'name': '承認者メールアドレス', 'is_date': False, 'required': True}
    }
    
    # エラーチェック
    missing_fields = []
    
    # 必須項目のチェック
    for field, rule in validation_rules.items():
        value = body.get(field)
        
        # 必須項目のチェック
        if rule['required'] and (not value or str(value).strip() == ""):
            missing_fields.append(f"{rule['name']}が未入力です")
            continue
            
        # 値が存在する場合のみ日付形式をチェック
        if value and rule['is_date'] and not is_valid_date(value):
            missing_fields.append(f"{rule['name']}の形式が不正です（YYYY-MM-DD形式で入力してください）")
    
    # エラーがある場合は通知メールを送信
    if missing_fields:
        error_message = "以下の項目に不備があります：\n" + "\n".join(f"- {field}" for field in missing_fields)
        send_email_via_ses(
            to_addresses=[body.get('mail', '不明')],
            subject="（ログ取得申請）入力内容に不備があります",
            body_text=(
                f"ログ取得申請の入力内容に不備があります。\n\n"
                f"{error_message}\n\n"
                f"正しい形式で再度申請してください。"
            )
        )
        raise ValueError(error_message)
    
    # 正常な場合は値を返す
    return (
        body['system'],
        datetime.strptime(body['from_date'], '%Y-%m-%d'),
        datetime.strptime(body['to_date'], '%Y-%m-%d') if body.get('to_date') else None,
        body['mail'],
        body['content'],
        body['approver']
    )
