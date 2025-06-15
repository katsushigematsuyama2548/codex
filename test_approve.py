import unittest
from unittest.mock import Mock, patch, MagicMock
import json
import os
from datetime import date, datetime
from pydantic import ValidationError
import boto3
from moto import mock_s3

# テスト対象のモジュールをインポート
from approve import (
    APIException, ExternalAPIException, ApprovalData,
    create_success_response, create_error_response,
    lambda_handler, validate_environment_variables,
    extract_and_validate_approval_data, get_email_body_from_s3,
    extract_reason, extract_log_period,
    create_teams_approval_html_message, create_teams_acceptance_html_message,
    create_correction_request_message, create_system_error_message,
    create_mailto_link, send_teams_approval_message,
    send_teams_acceptance_notification, send_error_notification,
    call_teams_api
)


class TestAPIException(unittest.TestCase):
    """APIException例外クラスのテスト"""
    
    def test_normal_api_exception_creation(self):
        """正常系: APIException作成"""
        # テストケース: 基本的な例外作成
        # リクエスト: status_code=400, message="Bad Request"
        # 期待値: 適切な属性設定
        exception = APIException(400, "Bad Request")
        self.assertEqual(exception.status_code, 400)
        self.assertEqual(exception.message, "Bad Request")
        self.assertEqual(str(exception), "Bad Request")


class TestExternalAPIException(unittest.TestCase):
    """ExternalAPIException例外クラスのテスト"""
    
    def test_normal_external_api_exception_with_details(self):
        """正常系: 詳細情報付きExternalAPIException作成"""
        # テストケース: 外部API詳細情報付き例外作成
        # リクエスト: status_code=502, message="API Error", external_status=404, external_message="Not Found"
        # 期待値: 詳細メッセージが構築される
        exception = ExternalAPIException(502, "API Error", 404, "Not Found")
        self.assertEqual(exception.status_code, 502)
        self.assertEqual(exception.external_status, 404)
        self.assertEqual(exception.external_message, "Not Found")
        self.assertIn("External API: 404 - Not Found", exception.message)
    
    def test_normal_external_api_exception_without_details(self):
        """正常系: 詳細情報なしExternalAPIException作成"""
        # テストケース: 外部API詳細情報なし例外作成
        # リクエスト: status_code=502, message="API Error"
        # 期待値: 基本メッセージのみ
        exception = ExternalAPIException(502, "API Error")
        self.assertEqual(exception.status_code, 502)
        self.assertEqual(exception.message, "API Error")


class TestApprovalData(unittest.TestCase):
    """ApprovalDataモデルのテスト"""
    
    def test_normal_approval_data_creation(self):
        """正常系: ApprovalData作成"""
        # テストケース: 有効なデータでApprovalData作成
        # リクエスト: 全フィールド有効値
        # 期待値: 正常にオブジェクト作成
        data = ApprovalData(
            mail="test@example.com",
            content="テスト申請",
            system="テストシステム",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 1, 2)
        )
        self.assertEqual(data.mail, "test@example.com")
        self.assertEqual(data.content, "テスト申請")
        self.assertEqual(data.system, "テストシステム")
        self.assertEqual(data.from_date, date(2024, 1, 1))
        self.assertEqual(data.to_date, date(2024, 1, 2))
    
    def test_error_invalid_email_format(self):
        """異常系: 無効なメールアドレス形式"""
        # テストケース: 無効なメールアドレス
        # リクエスト: mail="invalid-email"
        # 期待値: ValidationError発生
        with self.assertRaises(ValidationError):
            ApprovalData(
                mail="invalid-email",
                content="テスト申請",
                system="テストシステム",
                from_date=date(2024, 1, 1),
                to_date=date(2024, 1, 2)
            )
    
    def test_error_empty_content(self):
        """異常系: 空の申請内容"""
        # テストケース: 空の申請内容
        # リクエスト: content=""
        # 期待値: ValidationError発生
        with self.assertRaises(ValidationError):
            ApprovalData(
                mail="test@example.com",
                content="",
                system="テストシステム",
                from_date=date(2024, 1, 1),
                to_date=date(2024, 1, 2)
            )
    
    def test_error_content_too_long(self):
        """異常系: 申請内容が長すぎる"""
        # テストケース: 10000文字を超える申請内容
        # リクエスト: content="a" * 10001
        # 期待値: ValidationError発生
        with self.assertRaises(ValidationError):
            ApprovalData(
                mail="test@example.com",
                content="a" * 10001,
                system="テストシステム",
                from_date=date(2024, 1, 1),
                to_date=date(2024, 1, 2)
            )
    
    def test_error_empty_system(self):
        """異常系: 空のシステム名"""
        # テストケース: 空のシステム名
        # リクエスト: system=""
        # 期待値: ValidationError発生
        with self.assertRaises(ValidationError):
            ApprovalData(
                mail="test@example.com",
                content="テスト申請",
                system="",
                from_date=date(2024, 1, 1),
                to_date=date(2024, 1, 2)
            )
    
    def test_error_system_too_long(self):
        """異常系: システム名が長すぎる"""
        # テストケース: 200文字を超えるシステム名
        # リクエスト: system="a" * 201
        # 期待値: ValidationError発生
        with self.assertRaises(ValidationError):
            ApprovalData(
                mail="test@example.com",
                content="テスト申請",
                system="a" * 201,
                from_date=date(2024, 1, 1),
                to_date=date(2024, 1, 2)
            )
    
    def test_error_to_date_before_from_date(self):
        """異常系: 終了日が開始日より前"""
        # テストケース: 終了日 < 開始日
        # リクエスト: from_date=2024-01-02, to_date=2024-01-01
        # 期待値: ValidationError発生（カスタムバリデーター）
        with self.assertRaises(ValidationError) as cm:
            ApprovalData(
                mail="test@example.com",
                content="テスト申請",
                system="テストシステム",
                from_date=date(2024, 1, 2),
                to_date=date(2024, 1, 1)
            )
        self.assertIn("終了日は開始日以降の日付を指定してください", str(cm.exception))
    
    def test_error_extra_fields_forbidden(self):
        """異常系: 未定義フィールドの追加"""
        # テストケース: 未定義フィールド追加
        # リクエスト: extra_field="test"
        # 期待値: ValidationError発生
        with self.assertRaises(ValidationError):
            ApprovalData(
                mail="test@example.com",
                content="テスト申請",
                system="テストシステム",
                from_date=date(2024, 1, 1),
                to_date=date(2024, 1, 2),
                extra_field="test"
            )


class TestResponseFunctions(unittest.TestCase):
    """レスポンス作成関数のテスト"""
    
    def test_normal_create_success_response_default(self):
        """正常系: デフォルト成功レスポンス作成"""
        # テストケース: デフォルトメッセージで成功レスポンス作成
        # リクエスト: 引数なし
        # 期待値: statusCode=200, message="Success"
        response = create_success_response()
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["message"], "Success")
    
    def test_normal_create_success_response_custom(self):
        """正常系: カスタム成功レスポンス作成"""
        # テストケース: カスタムメッセージで成功レスポンス作成
        # リクエスト: message="カスタム成功"
        # 期待値: statusCode=200, message="カスタム成功"
        response = create_success_response("カスタム成功")
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["message"], "カスタム成功")
    
    def test_normal_create_error_response(self):
        """正常系: エラーレスポンス作成"""
        # テストケース: エラーレスポンス作成
        # リクエスト: status_code=400, message="Bad Request"
        # 期待値: statusCode=400, message="Bad Request"
        response = create_error_response(400, "Bad Request")
        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertEqual(body["message"], "Bad Request")


class TestValidateEnvironmentVariables(unittest.TestCase):
    """環境変数バリデーション関数のテスト"""
    
    def setUp(self):
        """テスト前の環境変数クリア"""
        self.env_vars = [
            'BUCKET_NAME', 'TEAMS_TEAM_NAME', 'TEAMS_CHANNEL_NAME',
            'ERROR_NOTIFICATION_TEAM_NAME', 'ERROR_NOTIFICATION_CHANNEL_NAME',
            'APPROVAL_SENDER_EMAIL'
        ]
        # 既存の環境変数を保存
        self.original_env = {}
        for var in self.env_vars:
            self.original_env[var] = os.environ.get(var)
            if var in os.environ:
                del os.environ[var]
    
    def tearDown(self):
        """テスト後の環境変数復元"""
        for var, value in self.original_env.items():
            if value is not None:
                os.environ[var] = value
            elif var in os.environ:
                del os.environ[var]
    
    def test_normal_all_env_vars_present(self):
        """正常系: 全環境変数が設定済み"""
        # テストケース: 必要な環境変数が全て設定されている
        # リクエスト: 全環境変数設定
        # 期待値: 例外発生なし
        for var in self.env_vars:
            os.environ[var] = f"test_{var.lower()}"
        
        # 例外が発生しないことを確認
        try:
            validate_environment_variables()
        except APIException:
            self.fail("validate_environment_variables() raised APIException unexpectedly!")
    
    def test_error_missing_single_env_var(self):
        """異常系: 単一環境変数が未設定"""
        # テストケース: BUCKET_NAMEのみ未設定
        # リクエスト: BUCKET_NAME未設定、他は設定済み
        # 期待値: APIException(500)発生、BUCKET_NAMEが含まれる
        for var in self.env_vars[1:]:  # BUCKET_NAME以外を設定
            os.environ[var] = f"test_{var.lower()}"
        
        with self.assertRaises(APIException) as cm:
            validate_environment_variables()
        
        self.assertEqual(cm.exception.status_code, 500)
        self.assertIn("BUCKET_NAME", cm.exception.message)
        self.assertIn("必要な環境変数が設定されていません", cm.exception.message)
    
    def test_error_missing_multiple_env_vars(self):
        """異常系: 複数環境変数が未設定"""
        # テストケース: BUCKET_NAMEとTEAMS_TEAM_NAMEが未設定
        # リクエスト: 2つの環境変数未設定
        # 期待値: APIException(500)発生、両方の変数名が含まれる
        for var in self.env_vars[2:]:  # 最初の2つ以外を設定
            os.environ[var] = f"test_{var.lower()}"
        
        with self.assertRaises(APIException) as cm:
            validate_environment_variables()
        
        self.assertEqual(cm.exception.status_code, 500)
        self.assertIn("BUCKET_NAME", cm.exception.message)
        self.assertIn("TEAMS_TEAM_NAME", cm.exception.message)
    
    def test_error_all_env_vars_missing(self):
        """異常系: 全環境変数が未設定"""
        # テストケース: 全環境変数が未設定
        # リクエスト: 環境変数なし
        # 期待値: APIException(500)発生、全変数名が含まれる
        with self.assertRaises(APIException) as cm:
            validate_environment_variables()
        
        self.assertEqual(cm.exception.status_code, 500)
        for var in self.env_vars:
            self.assertIn(var, cm.exception.message)


class TestExtractReason(unittest.TestCase):
    """申請理由抽出関数のテスト"""
    
    def test_normal_extract_reason_basic(self):
        """正常系: 基本的な申請理由抽出"""
        # テストケース: 標準的な形式の申請理由
        # リクエスト: 【申請理由】テスト理由【ログ取得期間】
        # 期待値: "テスト理由"
        body_text = "【申請理由】テスト理由【ログ取得期間】FROM: 2024-01-01 TO: 2024-01-02"
        result = extract_reason(body_text)
        self.assertEqual(result, "テスト理由")
    
    def test_normal_extract_reason_multiline(self):
        """正常系: 複数行の申請理由抽出"""
        # テストケース: 改行を含む申請理由
        # リクエスト: 【申請理由】理由1\n理由2【ログ取得期間】
        # 期待値: "理由1\n理由2"
        body_text = "【申請理由】理由1\n理由2【ログ取得期間】FROM: 2024-01-01 TO: 2024-01-02"
        result = extract_reason(body_text)
        self.assertEqual(result, "理由1\n理由2")
    
    def test_normal_extract_reason_with_spaces(self):
        """正常系: 前後にスペースがある申請理由抽出"""
        # テストケース: 前後にスペースがある申請理由
        # リクエスト: 【申請理由】  テスト理由  【ログ取得期間】
        # 期待値: "テスト理由"（スペース除去）
        body_text = "【申請理由】  テスト理由  【ログ取得期間】FROM: 2024-01-01 TO: 2024-01-02"
        result = extract_reason(body_text)
        self.assertEqual(result, "テスト理由")
    
    def test_error_no_reason_section(self):
        """異常系: 申請理由セクションなし"""
        # テストケース: 【申請理由】セクションが存在しない
        # リクエスト: 【ログ取得期間】のみ
        # 期待値: 空文字列
        body_text = "【ログ取得期間】FROM: 2024-01-01 TO: 2024-01-02"
        result = extract_reason(body_text)
        self.assertEqual(result, "")
    
    def test_error_no_log_period_section(self):
        """異常系: ログ取得期間セクションなし"""
        # テストケース: 【ログ取得期間】セクションが存在しない
        # リクエスト: 【申請理由】のみ
        # 期待値: 空文字列
        body_text = "【申請理由】テスト理由"
        result = extract_reason(body_text)
        self.assertEqual(result, "")
    
    def test_error_empty_reason(self):
        """異常系: 空の申請理由"""
        # テストケース: 申請理由が空
        # リクエスト: 【申請理由】【ログ取得期間】
        # 期待値: 空文字列
        body_text = "【申請理由】【ログ取得期間】FROM: 2024-01-01 TO: 2024-01-02"
        result = extract_reason(body_text)
        self.assertEqual(result, "")


class TestExtractLogPeriod(unittest.TestCase):
    """ログ取得期間抽出関数のテスト"""
    
    def test_normal_extract_actual_dates(self):
        """正常系: 実際の日付抽出"""
        # テストケース: 実際の日付形式
        # リクエスト: FROM: 2024-01-01 TO: 2024-01-02
        # 期待値: ("2024-01-01", "2024-01-02")
        body_text = "【ログ取得期間】FROM: 2024-01-01 TO: 2024-01-02"
        from_date, to_date = extract_log_period(body_text)
        self.assertEqual(from_date, "2024-01-01")
        self.assertEqual(to_date, "2024-01-02")
    
    def test_normal_extract_template_dates_double_quotes(self):
        """正常系: テンプレート日付抽出（ダブルクォート）"""
        # テストケース: テンプレート形式（ダブルクォート）
        # リクエスト: FROM: "yyyy-mm-dd" TO: "yyyy-mm-dd"
        # 期待値: 前日と今日の日付
        body_text = '【ログ取得期間】FROM: "yyyy-mm-dd" TO: "yyyy-mm-dd"'
        from_date, to_date = extract_log_period(body_text)
        
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        today = datetime.now().strftime("%Y-%m-%d")
        
        self.assertEqual(from_date, yesterday)
        self.assertEqual(to_date, today)
    
    def test_normal_extract_template_dates_single_quotes(self):
        """正常系: テンプレート日付抽出（シングルクォート）"""
        # テストケース: テンプレート形式（シングルクォート）
        # リクエスト: FROM: 'yyyy-mm-dd' TO: 'yyyy-mm-dd'
        # 期待値: 前日と今日の日付
        body_text = "【ログ取得期間】FROM: 'yyyy-mm-dd' TO: 'yyyy-mm-dd'"
        from_date, to_date = extract_log_period(body_text)
        
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        today = datetime.now().strftime("%Y-%m-%d")
        
        self.assertEqual(from_date, yesterday)
        self.assertEqual(to_date, today)
    
    def test_normal_extract_template_dates_no_quotes(self):
        """正常系: テンプレート日付抽出（クォートなし）"""
        # テストケース: テンプレート形式（クォートなし）
        # リクエスト: FROM: yyyy-mm-dd TO: yyyy-mm-dd
        # 期待値: 前日と今日の日付
        body_text = "【ログ取得期間】FROM: yyyy-mm-dd TO: yyyy-mm-dd"
        from_date, to_date = extract_log_period(body_text)
        
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        today = datetime.now().strftime("%Y-%m-%d")
        
        self.assertEqual(from_date, yesterday)
        self.assertEqual(to_date, today)
    
    def test_error_no_log_period_section(self):
        """異常系: ログ取得期間セクションなし"""
        # テストケース: 【ログ取得期間】セクションが存在しない
        # リクエスト: 【申請理由】のみ
        # 期待値: ValueError発生
        body_text = "【申請理由】テスト理由"
        with self.assertRaises(ValueError) as cm:
            extract_log_period(body_text)
        self.assertIn("【ログ取得期間】セクションが見つかりません", str(cm.exception))
    
    def test_error_no_dates_in_section(self):
        """異常系: 日付が記載されていない"""
        # テストケース: ログ取得期間セクションに日付なし
        # リクエスト: 【ログ取得期間】日付なし
        # 期待値: ValueError発生
        body_text = "【ログ取得期間】日付が記載されていません"
        with self.assertRaises(ValueError) as cm:
            extract_log_period(body_text)
        self.assertIn("ログ取得期間に日付が記載されていません", str(cm.exception))
    
    def test_error_only_one_date(self):
        """異常系: 日付が1つしかない"""
        # テストケース: 日付が1つのみ
        # リクエスト: 【ログ取得期間】2024-01-01
        # 期待値: ValueError発生
        body_text = "【ログ取得期間】2024-01-01"
        with self.assertRaises(ValueError) as cm:
            extract_log_period(body_text)
        self.assertIn("ログ取得期間に日付が1つしか記載されていません", str(cm.exception))
    
    def test_error_mixed_date_formats(self):
        """異常系: 日付形式の混在"""
        # テストケース: 実際の日付とテンプレートの混在
        # リクエスト: FROM: 2024-01-01 TO: "yyyy-mm-dd"
        # 期待値: ValueError発生
        body_text = '【ログ取得期間】FROM: 2024-01-01 TO: "yyyy-mm-dd"'
        with self.assertRaises(ValueError) as cm:
            extract_log_period(body_text)
        self.assertIn("ログ取得期間の日付形式が混在しています", str(cm.exception))
    
    def test_normal_multiple_dates_warning(self):
        """正常系: 3つ以上の日付（警告付き）"""
        # テストケース: 3つ以上の日付が存在（最初の2つを使用）
        # リクエスト: 2024-01-01 2024-01-02 2024-01-03
        # 期待値: 最初の2つを使用、警告ログ
        body_text = "【ログ取得期間】2024-01-01 2024-01-02 2024-01-03"
        
        with patch('approve.logger') as mock_logger:
            from_date, to_date = extract_log_period(body_text)
            
            self.assertEqual(from_date, "2024-01-01")
            self.assertEqual(to_date, "2024-01-02")
            
            # 警告ログが出力されることを確認
            mock_logger.warning.assert_called_once()
            warning_call = mock_logger.warning.call_args[0][0]
            self.assertIn("LOG_PERIOD_MULTIPLE_DATES", warning_call)


class TestExtractAndValidateApprovalData(unittest.TestCase):
    """申請データ抽出・バリデーション関数のテスト"""
    
    def test_normal_extract_and_validate_success(self):
        """正常系: 申請データ抽出・バリデーション成功"""
        # テストケース: 有効な申請データ
        # リクエスト: 完全な申請メール
        # 期待値: ApprovalDataオブジェクト作成成功
        mail_body = """
        【申請理由】
        テスト申請です
        【ログ取得期間】
        FROM: 2024-01-01
        TO: 2024-01-02
        """
        subject = "テストシステム"
        sender = "test@example.com"
        
        result = extract_and_validate_approval_data(mail_body, subject, sender)
        
        self.assertIsInstance(result, ApprovalData)
        self.assertEqual(result.mail, "test@example.com")
        self.assertEqual(result.content, "テスト申請です")
        self.assertEqual(result.system, "テストシステム")
        self.assertEqual(result.from_date, date(2024, 1, 1))
        self.assertEqual(result.to_date, date(2024, 1, 2))
    
    def test_error_no_reason_found(self):
        """異常系: 申請理由が見つからない"""
        # テストケース: 【申請理由】セクションなし
        # リクエスト: 申請理由なしのメール
        # 期待値: APIException(400)発生
        mail_body = """
        【ログ取得期間】
        FROM: 2024-01-01
        TO: 2024-01-02
        """
        subject = "テストシステム"
        sender = "test@example.com"
        
        with self.assertRaises(APIException) as cm:
            extract_and_validate_approval_data(mail_body, subject, sender)
        
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("【申請理由】の記載がありません", cm.exception.message)
    
    def test_error_invalid_date_format(self):
        """異常系: 無効な日付形式"""
        # テストケース: 無効な日付形式
        # リクエスト: 不正な日付形式
        # 期待値: APIException(400)発生
        mail_body = """
        【申請理由】
        テスト申請です
        【ログ取得期間】
        FROM: invalid-date
        TO: 2024-01-02
        """
        subject = "テストシステム"
        sender = "test@example.com"
        
        with self.assertRaises(APIException) as cm:
            extract_and_validate_approval_data(mail_body, subject, sender)
        
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("日付形式が不正です", cm.exception.message)
    
    def test_error_invalid_email_sender(self):
        """異常系: 無効な送信者メールアドレス"""
        # テストケース: 無効なメールアドレス形式
        # リクエスト: sender="invalid-email"
        # 期待値: APIException(400)発生
        mail_body = """
        【申請理由】
        テスト申請です
        【ログ取得期間】
        FROM: 2024-01-01
        TO: 2024-01-02
        """
        subject = "テストシステム"
        sender = "invalid-email"
        
        with self.assertRaises(APIException) as cm:
            extract_and_validate_approval_data(mail_body, subject, sender)
        
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("申請データのバリデーションに失敗しました", cm.exception.message)
    
    def test_error_date_range_validation(self):
        """異常系: 日付範囲バリデーションエラー"""
        # テストケース: 終了日が開始日より前
        # リクエスト: to_date < from_date
        # 期待値: APIException(400)発生
        mail_body = """
        【申請理由】
        テスト申請です
        【ログ取得期間】
        FROM: 2024-01-02
        TO: 2024-01-01
        """
        subject = "テストシステム"
        sender = "test@example.com"
        
        with self.assertRaises(APIException) as cm:
            extract_and_validate_approval_data(mail_body, subject, sender)
        
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("終了日は開始日以降の日付を指定してください", cm.exception.message)


class TestGetEmailBodyFromS3(unittest.TestCase):
    """S3メール本文取得関数のテスト"""
    
    def setUp(self):
        """テスト前の環境変数設定"""
        os.environ['BUCKET_NAME'] = 'test-bucket'
    
    def tearDown(self):
        """テスト後の環境変数クリア"""
        if 'BUCKET_NAME' in os.environ:
            del os.environ['BUCKET_NAME']
    
    @mock_s3
    def test_normal_get_email_body_simple_text(self):
        """正常系: シンプルなテキストメール取得"""
        # テストケース: シンプルなテキストメール
        # リクエスト: message_id="test-message-id"
        # 期待値: メール本文取得成功
        
        # S3モックセットアップ
        s3_client = boto3.client('s3', region_name='us-east-1')
        s3_client.create_bucket(Bucket='test-bucket')
        
        # シンプルなメールコンテンツ
        email_content = b"""From: test@example.com
To: recipient@example.com
Subject: Test Subject
Content-Type: text/plain; charset=utf-8

Test email body content"""
        
        s3_client.put_object(
            Bucket='test-bucket',
            Key='receive/test-message-id',
            Body=email_content
        )
        
        result = get_email_body_from_s3('test-message-id')
        self.assertEqual(result, "Test email body content")
    
    @mock_s3
    def test_normal_get_email_body_multipart(self):
        """正常系: マルチパートメール取得"""
        # テストケース: マルチパートメール
        # リクエスト: text/plainパートを含むマルチパートメール
        # 期待値: text/plainパートの本文取得
        
        # S3モックセットアップ
        s3_client = boto3.client('s3', region_name='us-east-1')
        s3_client.create_bucket(Bucket='test-bucket')
        
        # マルチパートメールコンテンツ
        email_content = b"""From: test@example.com
To: recipient@example.com
Subject: Test Subject
Content-Type: multipart/mixed; boundary="boundary123"

--boundary123
Content-Type: text/plain; charset=utf-8

Plain text content
--boundary123
Content-Type: text/html; charset=utf-8

<html><body>HTML content</body></html>
--boundary123--"""
        
        s3_client.put_object(
            Bucket='test-bucket',
            Key='receive/test-message-id',
            Body=email_content
        )
        
        result = get_email_body_from_s3('test-message-id')
        self.assertEqual(result, "Plain text content")
    
    def test_error_no_bucket_name_env(self):
        """異常系: BUCKET_NAME環境変数なし"""
        # テストケース: BUCKET_NAME環境変数が設定されていない
        # リクエスト: BUCKET_NAME未設定
        # 期待値: APIException(500)発生
        if 'BUCKET_NAME' in os.environ:
            del os.environ['BUCKET_NAME']
        
        with self.assertRaises(APIException) as cm:
            get_email_body_from_s3('test-message-id')
        
        self.assertEqual(cm.exception.status_code, 500)
        self.assertIn("BUCKET_NAME環境変数が設定されていません", cm.exception.message)
    
    @mock_s3
    def test_error_s3_object_not_found(self):
        """異常系: S3オブジェクトが存在しない"""
        # テストケース: 指定されたメッセージIDのオブジェクトが存在しない
        # リクエスト: 存在しないmessage_id
        # 期待値: APIException(500)発生
        
        # S3モックセットアップ（空のバケット）
        s3_client = boto3.client('s3', region_name='us-east-1')
        s3_client.create_bucket(Bucket='test-bucket')
        
        with self.assertRaises(APIException) as cm:
            get_email_body_from_s3('non-existent-message-id')
        
        self.assertEqual(cm.exception.status_code, 500)
        self.assertIn("S3からのメール取得に失敗しました", cm.exception.message)


class TestCallTeamsApi(unittest.TestCase):
    """Teams API呼び出し関数のテスト"""
    
    @patch('approve.http')
    def test_normal_call_teams_api_success(self):
        """正常系: Teams API呼び出し成功"""
        # テストケース: 正常なAPI呼び出し
        # リクエスト: 有効なteams_data
        # 期待値: レスポンスデータ返却
        
        # モックレスポンス設定
        mock_response = Mock()
        mock_response.status = 200
        mock_response.data = b'{"success": true, "message": "Message sent"}'
        
        mock_http = Mock()
        mock_http.request.return_value = mock_response
        
        with patch('approve.http', mock_http):
            teams_data = {
                "mode": 2,
                "team_name": "Test Team",
                "channel_name": "Test Channel",
                "message_text": "Test Message"
            }
            
            result = call_teams_api(teams_data)
            
            self.assertEqual(result, {"success": True, "message": "Message sent"})
            
            # HTTP呼び出しの確認
            mock_http.request.assert_called_once()
            call_args = mock_http.request.call_args
            self.assertEqual(call_args[0][0], "POST")  # method
            self.assertEqual(call_args[0][1], "https://tumr4jppl1.execute-api.ap-northeast-1.amazonaws.com/dev/teams/message")  # URL
    
    @patch('approve.http')
    def test_normal_call_teams_api_success_201(self):
        """正常系: Teams API呼び出し成功（201）"""
        # テストケース: 201ステータスでの成功
        # リクエスト: 有効なteams_data
        # 期待値: レスポンスデータ返却
        
        mock_response = Mock()
        mock_response.status = 201
        mock_response.data = b'{"created": true}'
        
        mock_http = Mock()
        mock_http.request.return_value = mock_response
        
        with patch('approve.http', mock_http):
            teams_data = {"mode": 2, "team_name": "Test Team"}
            result = call_teams_api(teams_data)
            self.assertEqual(result, {"created": True})
    
    @patch('approve.http')
    def test_normal_call_teams_api_empty_response(self):
        """正常系: Teams API呼び出し成功（空レスポンス）"""
        # テストケース: 空のレスポンスボディ
        # リクエスト: 有効なteams_data
        # 期待値: 空辞書返却
        
        mock_response = Mock()
        mock_response.status = 200
        mock_response.data = b''
        
        mock_http = Mock()
        mock_http.request.return_value = mock_response
        
        with patch('approve.http', mock_http):
            teams_data = {"mode": 2, "team_name": "Test Team"}
            result = call_teams_api(teams_data)
            self.assertEqual(result, {})
    
    @patch('approve.http')
    def test_error_call_teams_api_404(self):
        """異常系: Teams API 404エラー"""
        # テストケース: 404エラーレスポンス
        # リクエスト: 存在しないリソース
        # 期待値: APIException(502)発生
        
        mock_response = Mock()
        mock_response.status = 404
        mock_response.data = b'{"message": "User not found: test@example.com"}'
        
        mock_http = Mock()
        mock_http.request.return_value = mock_response
        
        with patch('approve.http', mock_http):
            teams_data = {"mode": 2, "team_name": "Test Team"}
            
            with self.assertRaises(APIException) as cm:
                call_teams_api(teams_data)
            
            self.assertEqual(cm.exception.status_code, 502)
            self.assertIn("Status:404", cm.exception.message)
            self.assertIn("User not found: test@example.com", cm.exception.message)
    
    @patch('approve.http')
    def test_error_call_teams_api_500(self):
        """異常系: Teams API 500エラー"""
        # テストケース: 500エラーレスポンス
        # リクエスト: サーバーエラー
        # 期待値: APIException(502)発生
        
        mock_response = Mock()
        mock_response.status = 500
        mock_response.data = b'{"message": "Internal server error"}'
        
        mock_http = Mock()
        mock_http.request.return_value = mock_response
        
        with patch('approve.http', mock_http):
            teams_data = {"mode": 2, "team_name": "Test Team"}
            
            with self.assertRaises(APIException) as cm:
                call_teams_api(teams_data)
            
            self.assertEqual(cm.exception.status_code, 502)
            self.assertIn("Status:500", cm.exception.message)
            self.assertIn("Internal server error", cm.exception.message)
    
    @patch('approve.http')
    def test_error_call_teams_api_invalid_json(self):
        """異常系: Teams API 無効なJSONレスポンス"""
        # テストケース: 無効なJSONレスポンス
        # リクエスト: 不正なJSONレスポンス
        # 期待値: APIException(502)発生
        
        mock_response = Mock()
        mock_response.status = 400
        mock_response.data = b'invalid json'
        
        mock_http = Mock()
        mock_http.request.return_value = mock_response
        
        with patch('approve.http', mock_http):
            teams_data = {"mode": 2, "team_name": "Test Team"}
            
            with self.assertRaises(APIException) as cm:
                call_teams_api(teams_data)
            
            self.assertEqual(cm.exception.status_code, 502)
            self.assertIn("Status:400 - Invalid JSON response", cm.exception.message)
    
    @patch('approve.http')
    def test_error_call_teams_api_connection_error(self):
        """異常系: Teams API 接続エラー"""
        # テストケース: 接続エラー
        # リクエスト: ネットワークエラー
        # 期待値: APIException(502)発生
        
        mock_http = Mock()
        mock_http.request.side_effect = Exception("Connection failed")
        
        with patch('approve.http', mock_http):
            teams_data = {"mode": 2, "team_name": "Test Team"}
            
            with self.assertRaises(APIException) as cm:
                call_teams_api(teams_data)
            
            self.assertEqual(cm.exception.status_code, 502)
            self.assertIn("Teams API通信エラー", cm.exception.message)
            self.assertIn("Connection failed", cm.exception.message)


class TestHtmlMessageCreation(unittest.TestCase):
    """HTMLメッセージ作成関数のテスト"""
    
    def setUp(self):
        """テスト用ApprovalDataオブジェクト作成"""
        self.approval_data = ApprovalData(
            mail="test@example.com",
            content="テスト申請内容\n複数行テスト",
            system="テストシステム",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 1, 2)
        )
        self.period_str = "FROM: 2024-01-01 TO: 2024-01-02"
        self.draft_link = "mailto:approver@example.com?subject=test&body=test"
    
    def test_normal_create_teams_approval_html_message(self):
        """正常系: Teams承認用HTMLメッセージ作成"""
        # テストケース: 承認用HTMLメッセージ作成
        # リクエスト: ApprovalData, period_str, draft_link
        # 期待値: HTMLテーブル形式のメッセージ
        
        result = create_teams_approval_html_message(
            self.approval_data, self.period_str, self.draft_link
        )
        
        # HTMLテーブルの存在確認
        self.assertIn('<table border="1"', result)
        self.assertIn('<strong>申請システム</strong>', result)
        self.assertIn('<strong>申請者</strong>', result)
        self.assertIn('<strong>申請内容</strong>', result)
        self.assertIn('<strong>ログ取得期間</strong>', result)
        
        # データの存在確認
        self.assertIn("テストシステム", result)
        self.assertIn("test@example.com", result)
        self.assertIn("テスト申請内容<br>複数行テスト", result)  # 改行がHTMLに変換
        self.assertIn("FROM: 2024-01-01 TO: 2024-01-02", result)
        
        # 承認メールリンクの存在確認
        self.assertIn('🔗 承認メール作成:', result)
        self.assertIn('📧 承認メールを作成する', result)
        self.assertIn(self.draft_link, result)
    
    def test_normal_create_teams_acceptance_html_message(self):
        """正常系: Teams受付通知用HTMLメッセージ作成"""
        # テストケース: 受付通知用HTMLメッセージ作成
        # リクエスト: ApprovalData, period_str
        # 期待値: HTMLテーブル形式の受付通知メッセージ
        
        result = create_teams_acceptance_html_message(
            self.approval_data, self.period_str
        )
        
        # HTMLテーブルの存在確認
        self.assertIn('<table border="1"', result)
        self.assertIn('<strong>申請システム</strong>', result)
        self.assertIn('<strong>申請内容</strong>', result)
        self.assertIn('<strong>ログ取得期間</strong>', result)
        
        # データの存在確認
        self.assertIn("テストシステム", result)
        self.assertIn("テスト申請内容<br>複数行テスト", result)
        self.assertIn("FROM: 2024-01-01 TO: 2024-01-02", result)
        
        # 受付完了メッセージの確認
        self.assertIn("申請を受け付けました", result)
        self.assertIn("承認者による確認後", result)
    
    def test_normal_create_correction_request_message(self):
        """正常系: 修正依頼メッセージ作成"""
        # テストケース: 修正依頼メッセージ作成
        # リクエスト: error_message, sender_email, mail_subject
        # 期待値: HTMLテーブル形式の修正依頼メッセージ
        
        error_message = "日付形式が不正です"
        sender_email = "test@example.com"
        mail_subject = "テストシステム"
        
        result = create_correction_request_message(error_message, sender_email, mail_subject)
        
        # HTMLテーブルの存在確認
        self.assertIn('<table border="1"', result)
        self.assertIn('<strong>申請システム</strong>', result)
        self.assertIn('<strong>エラー内容</strong>', result)
        
        # データの存在確認
        self.assertIn("テストシステム", result)
        self.assertIn("日付形式が不正です", result)
        
        # 修正方法の説明確認
        self.assertIn("修正方法:", result)
        self.assertIn("【申請理由】", result)
        self.assertIn("【ログ取得期間】", result)
        self.assertIn("FROM: YYYY-MM-DD", result)
        self.assertIn("TO: YYYY-MM-DD", result)
    
    def test_normal_create_system_error_message(self):
        """正常系: システムエラーメッセージ作成"""
        # テストケース: システムエラーメッセージ作成
        # リクエスト: sender_email, mail_subject
        # 期待値: HTMLテーブル形式のシステムエラーメッセージ
        
        sender_email = "test@example.com"
        mail_subject = "テストシステム"
        
        result = create_system_error_message(sender_email, mail_subject)
        
        # HTMLテーブルの存在確認
        self.assertIn('<table border="1"', result)
        self.assertIn('<strong>申請システム</strong>', result)
        self.assertIn('<strong>エラー</strong>', result)
        
        # データの存在確認
        self.assertIn("テストシステム", result)
        self.assertIn("想定外のエラーが発生しました", result)
        
        # SD課への依頼メッセージ確認
        self.assertIn("SD課への依頼をお願いします", result)
        self.assertIn("手動でのログ取得対応", result)


class TestCreateMailtoLink(unittest.TestCase):
    """メール下書きリンク作成関数のテスト"""
    
    def setUp(self):
        """テスト前の環境変数設定"""
        os.environ['APPROVAL_SENDER_EMAIL'] = 'approver@example.com'
        self.approval_data = ApprovalData(
            mail="test@example.com",
            content="テスト申請内容\r\n複数行テスト",
            system="テストシステム",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 1, 2)
        )
    
    def tearDown(self):
        """テスト後の環境変数クリア"""
        if 'APPROVAL_SENDER_EMAIL' in os.environ:
            del os.environ['APPROVAL_SENDER_EMAIL']
    
    def test_normal_create_mailto_link(self):
        """正常系: メール下書きリンク作成"""
        # テストケース: 正常なメール下書きリンク作成
        # リクエスト: ApprovalData
        # 期待値: mailto形式のリンク
        
        result = create_mailto_link(self.approval_data)
        
        # mailto形式の確認
        self.assertTrue(result.startswith("mailto:approver@example.com?"))
        
        # URLデコードして内容確認
        import urllib.parse
        parsed = urllib.parse.urlparse(result)
        query_params = urllib.parse.parse_qs(parsed.query)
        
        # 件名の確認
        self.assertIn("subject", query_params)
        subject = urllib.parse.unquote(query_params["subject"][0])
        self.assertEqual(subject, "ログ取得API実行: テストシステム")
        
        # 本文の確認
        self.assertIn("body", query_params)
        body = urllib.parse.unquote(query_params["body"][0])
        body_json = json.loads(body)
        
        self.assertEqual(body_json["mail"], "test@example.com")
        self.assertEqual(body_json["content"], "テスト申請内容\n複数行テスト")  # 改行正規化
        self.assertEqual(body_json["system"], "テストシステム")
        self.assertEqual(body_json["from_date"], "2024-01-01")
        self.assertEqual(body_json["to_date"], "2024-01-02")
    
    def test_error_no_approval_sender_email_env(self):
        """異常系: APPROVAL_SENDER_EMAIL環境変数なし"""
        # テストケース: APPROVAL_SENDER_EMAIL環境変数が設定されていない
        # リクエスト: APPROVAL_SENDER_EMAIL未設定
        # 期待値: APIException(500)発生
        
        if 'APPROVAL_SENDER_EMAIL' in os.environ:
            del os.environ['APPROVAL_SENDER_EMAIL']
        
        with self.assertRaises(APIException) as cm:
            create_mailto_link(self.approval_data)
        
        self.assertEqual(cm.exception.status_code, 500)
        self.assertIn("APPROVAL_SENDER_EMAIL環境変数が設定されていません", cm.exception.message)


class TestSendTeamsApprovalMessage(unittest.TestCase):
    """Teams承認メッセージ送信関数のテスト"""
    
    def setUp(self):
        """テスト前の環境変数設定"""
        os.environ['TEAMS_TEAM_NAME'] = 'Test Team'
        os.environ['TEAMS_CHANNEL_NAME'] = 'Test Channel'
        os.environ['APPROVAL_SENDER_EMAIL'] = 'approver@example.com'
        
        self.approval_data = ApprovalData(
            mail="test@example.com",
            content="テスト申請内容",
            system="テストシステム",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 1, 2)
        )
    
    def tearDown(self):
        """テスト後の環境変数クリア"""
        env_vars = ['TEAMS_TEAM_NAME', 'TEAMS_CHANNEL_NAME', 'APPROVAL_SENDER_EMAIL']
        for var in env_vars:
            if var in os.environ:
                del os.environ[var]
    
    @patch('approve.call_teams_api')
    def test_normal_send_teams_approval_message(self, mock_call_teams_api):
        """正常系: Teams承認メッセージ送信成功"""
        # テストケース: 正常な承認メッセージ送信
        # リクエスト: ApprovalData
        # 期待値: call_teams_apiが適切なパラメータで呼び出される
        
        # モック設定
        mock_call_teams_api.return_value = {"success": True}
        
        result = send_teams_approval_message(self.approval_data)
        
        # 戻り値の確認
        self.assertEqual(result, {"success": True})
        
        # call_teams_apiの呼び出し確認
        mock_call_teams_api.assert_called_once()
        call_args = mock_call_teams_api.call_args[0][0]
        
        self.assertEqual(call_args["mode"], 2)
        self.assertEqual(call_args["team_name"], "Test Team")
        self.assertEqual(call_args["channel_name"], "Test Channel")
        self.assertEqual(call_args["content_type"], "html")
        self.assertEqual(call_args["subject"], "ログ取得の申請：API承認依頼")
        self.assertIn("テストシステム", call_args["message_text"])
        self.assertIn("test@example.com", call_args["message_text"])
    
    @patch('approve.call_teams_api')
    def test_error_send_teams_approval_message_api_failure(self, mock_call_teams_api):
        """異常系: Teams API呼び出しでエラー発生"""
        # テストケース: Teams API呼び出しでエラー発生
        # リクエスト: ApprovalData
        # 期待値: APIException(502)
        
        # モック設定（例外発生）
        mock_call_teams_api.side_effect = APIException(502, "Teams API error")
        
        with self.assertRaises(APIException) as cm:
            send_teams_approval_message(self.approval_data)
        
        self.assertEqual(cm.exception.status_code, 502)
        self.assertIn("Teams API error", cm.exception.message)


class TestSendTeamsAcceptanceNotification(unittest.TestCase):
    """Teams受付通知送信関数のテスト"""
    
    def setUp(self):
        """テスト前の環境変数設定"""
        os.environ['ERROR_NOTIFICATION_TEAM_NAME'] = 'Error Team'
        os.environ['ERROR_NOTIFICATION_CHANNEL_NAME'] = 'Error Channel'
        
        self.approval_data = ApprovalData(
            mail="test@example.com",
            content="テスト申請内容",
            system="テストシステム",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 1, 2)
        )
    
    def tearDown(self):
        """テスト後の環境変数クリア"""
        env_vars = ['ERROR_NOTIFICATION_TEAM_NAME', 'ERROR_NOTIFICATION_CHANNEL_NAME']
        for var in env_vars:
            if var in os.environ:
                del os.environ[var]
    
    @patch('approve.call_teams_api')
    def test_normal_send_teams_acceptance_notification(self, mock_call_teams_api):
        """正常系: Teams受付通知送信成功"""
        # テストケース: 正常な受付通知送信
        # リクエスト: ApprovalData
        # 期待値: call_teams_apiが適切なパラメータで呼び出される
        
        # モック設定
        mock_call_teams_api.return_value = {"success": True}
        
        result = send_teams_acceptance_notification(self.approval_data)
        
        # 戻り値の確認
        self.assertEqual(result, {"success": True})
        
        # call_teams_apiの呼び出し確認
        mock_call_teams_api.assert_called_once()
        call_args = mock_call_teams_api.call_args[0][0]
        
        self.assertEqual(call_args["mode"], 2)
        self.assertEqual(call_args["team_name"], "Error Team")
        self.assertEqual(call_args["channel_name"], "Error Channel")
        self.assertEqual(call_args["content_type"], "html")
        self.assertEqual(call_args["subject"], "ログ取得の申請：受付完了")
        
        # メンション設定の確認
        self.assertEqual(len(call_args["mentions"]), 1)
        self.assertEqual(call_args["mentions"][0]["mention_type"], "user")
        self.assertEqual(call_args["mentions"][0]["email_address"], "test@example.com")
    
    @patch('approve.call_teams_api')
    def test_error_send_teams_acceptance_notification_api_failure(self, mock_call_teams_api):
        """異常系: Teams API呼び出しでエラー発生"""
        # テストケース: Teams API呼び出しでエラー発生
        # リクエスト: ApprovalData
        # 期待値: APIException(502)
        
        # モック設定（例外発生）
        mock_call_teams_api.side_effect = APIException(502, "Teams API error")
        
        with self.assertRaises(APIException) as cm:
            send_teams_acceptance_notification(self.approval_data)
        
        self.assertEqual(cm.exception.status_code, 502)
        self.assertIn("Teams API error", cm.exception.message)


class TestSendErrorNotification(unittest.TestCase):
    """エラー通知送信関数のテスト"""
    
    def setUp(self):
        """テスト前の環境変数設定"""
        os.environ['ERROR_NOTIFICATION_TEAM_NAME'] = 'Error Team'
        os.environ['ERROR_NOTIFICATION_CHANNEL_NAME'] = 'Error Channel'
    
    def tearDown(self):
        """テスト後の環境変数クリア"""
        env_vars = ['ERROR_NOTIFICATION_TEAM_NAME', 'ERROR_NOTIFICATION_CHANNEL_NAME']
        for var in env_vars:
            if var in os.environ:
                del os.environ[var]
    
    @patch('approve.call_teams_api')
    def test_normal_send_error_notification_client_error(self, mock_call_teams_api):
        """正常系: クライアントエラー通知送信（400番台）"""
        # テストケース: 400番台エラーの通知送信
        # リクエスト: APIException(400), sender_email, mail_subject
        # 期待値: 修正依頼メッセージが送信される
        
        # モック設定
        mock_call_teams_api.return_value = {"success": True}
        
        error = APIException(400, "日付形式が不正です")
        sender_email = "test@example.com"
        mail_subject = "テストシステム"
        
        with patch('approve.call_teams_api', mock_call_teams_api):
            result = send_error_notification(error, sender_email, mail_subject)
            
            # 戻り値の確認
            self.assertEqual(result, {"success": True})
            
            # call_teams_apiの呼び出し確認
            mock_call_teams_api.assert_called_once()
            call_args = mock_call_teams_api.call_args[0]
            
            self.assertEqual(call_args[0]["subject"], "ログ取得の申請：申請内容の修正が必要です")
            self.assertIn("日付形式が不正です", call_args[0]["message_text"])
            self.assertIn("修正方法:", call_args[0]["message_text"])
    
    @patch('approve.call_teams_api')
    def test_normal_send_error_notification_server_error(self, mock_call_teams_api):
        """正常系: サーバーエラー通知送信（500番台）"""
        # テストケース: 500番台エラーの通知送信
        # リクエスト: APIException(500), sender_email, mail_subject
        # 期待値: システムエラーメッセージが送信される
        
        # モック設定
        mock_call_teams_api.return_value = {"success": True}
        
        error = APIException(500, "システムエラーが発生しました")
        sender_email = "test@example.com"
        mail_subject = "テストシステム"
        
        with patch('approve.call_teams_api', mock_call_teams_api):
            result = send_error_notification(error, sender_email, mail_subject)
            
            # 戻り値の確認
            self.assertEqual(result, {"success": True})
            
            # call_teams_apiの呼び出し確認
            mock_call_teams_api.assert_called_once()
            call_args = mock_call_teams_api.call_args[0]
            
            self.assertEqual(call_args[0]["subject"], "ログ取得の申請：システムエラーが発生しました")
            self.assertIn("想定外のエラーが発生しました", call_args[0]["message_text"])
            self.assertIn("SD課への依頼をお願いします", call_args[0]["message_text"])
    
    @patch('approve.call_teams_api')
    def test_error_send_error_notification_api_failure(self, mock_call_teams_api):
        """異常系: エラー通知送信でAPI失敗"""
        # テストケース: エラー通知送信時にAPI呼び出しが失敗
        # リクエスト: APIException, sender_email, mail_subject
        # 期待値: Noneを返す（メイン処理は継続）
        
        # モック設定（例外発生）
        mock_call_teams_api.side_effect = APIException(404, "Team not found")
        
        error = APIException(400, "テストエラー")
        sender_email = "test@example.com"
        mail_subject = "テストシステム"
        
        with patch('approve.call_teams_api', mock_call_teams_api):
            result = send_error_notification(error, sender_email, mail_subject)
            
            # エラー通知失敗時はNoneを返す
            self.assertIsNone(result)


class TestLambdaHandler(unittest.TestCase):
    """Lambda メインハンドラー関数のテスト"""
    
    def setUp(self):
        """テスト前の環境変数設定"""
        self.env_vars = {
            'BUCKET_NAME': 'test-bucket',
            'TEAMS_TEAM_NAME': 'Test Team',
            'TEAMS_CHANNEL_NAME': 'Test Channel',
            'ERROR_NOTIFICATION_TEAM_NAME': 'Error Team',
            'ERROR_NOTIFICATION_CHANNEL_NAME': 'Error Channel',
            'APPROVAL_SENDER_EMAIL': 'approver@example.com'
        }
        for key, value in self.env_vars.items():
            os.environ[key] = value
        
        # 標準的なSESイベント
        self.valid_ses_event = {
            "Records": [{
                "ses": {
                    "mail": {
                        "messageId": "test-message-id",
                        "commonHeaders": {
                            "subject": "テストシステム"
                        },
                        "source": "test@example.com"
                    }
                }
            }]
        }
        
        # モックコンテキスト
        self.mock_context = Mock()
        self.mock_context.aws_request_id = "test-request-id"
    
    def tearDown(self):
        """テスト後の環境変数クリア"""
        for key in self.env_vars.keys():
            if key in os.environ:
                del os.environ[key]
    
    @patch('approve.send_teams_acceptance_notification')
    @patch('approve.send_teams_approval_message')
    @patch('approve.extract_and_validate_approval_data')
    @patch('approve.get_email_body_from_s3')
    def test_normal_lambda_handler_success(self, mock_get_email, mock_extract_validate, 
                                         mock_send_approval, mock_send_acceptance):
        """正常系: Lambda ハンドラー成功"""
        # テストケース: 全処理が正常に完了
        # リクエスト: 有効なSESイベント
        # 期待値: 成功レスポンス
        
        # モック設定
        mock_get_email.return_value = "【申請理由】テスト申請【ログ取得期間】FROM: 2024-01-01 TO: 2024-01-02"
        mock_extract_validate.return_value = ApprovalData(
            mail="test@example.com",
            content="テスト申請",
            system="テストシステム",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 1, 2)
        )
        mock_send_approval.return_value = {"success": True}
        mock_send_acceptance.return_value = {"success": True}
        
        result = lambda_handler(self.valid_ses_event, self.mock_context)
        
        # レスポンスの確認
        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["message"], "承認依頼を正常に送信しました")
        
        # 各関数の呼び出し確認
        mock_get_email.assert_called_once_with("test-message-id")
        mock_extract_validate.assert_called_once()
        mock_send_approval.assert_called_once()
        mock_send_acceptance.assert_called_once()
    
    def test_error_lambda_handler_invalid_ses_event_structure(self):
        """異常系: 無効なSESイベント構造"""
        # テストケース: SESイベントの構造が不正
        # リクエスト: Records配列なし
        # 期待値: APIException(400)
        
        invalid_event = {"invalid": "structure"}
        
        result = lambda_handler(invalid_event, self.mock_context)
        
        self.assertEqual(result["statusCode"], 400)
        body = json.loads(result["body"])
        self.assertIn("SESイベント形式が不正です", body["message"])
    
    def test_error_lambda_handler_missing_ses_fields(self):
        """異常系: SESイベントの必須フィールド不足"""
        # テストケース: messageIdが不足
        # リクエスト: messageId不足のSESイベント
        # 期待値: APIException(400)
        
        invalid_event = {
            "Records": [{
                "ses": {
                    "mail": {
                        "commonHeaders": {
                            "subject": "テストシステム"
                        },
                        "source": "test@example.com"
                    }
                }
            }]
        }
        
        result = lambda_handler(invalid_event, self.mock_context)
        
        self.assertEqual(result["statusCode"], 400)
        body = json.loads(result["body"])
        self.assertIn("SESイベント形式が不正です", body["message"])
    
    @patch('approve.get_email_body_from_s3')
    def test_error_lambda_handler_s3_failure(self, mock_get_email):
        """異常系: S3からのメール取得失敗"""
        # テストケース: S3からのメール取得でエラー
        # リクエスト: 有効なSESイベント
        # 期待値: APIException(500)
        
        # モック設定（例外発生）
        mock_get_email.side_effect = APIException(500, "S3からのメール取得に失敗しました")
        
        result = lambda_handler(self.valid_ses_event, self.mock_context)
        
        self.assertEqual(result["statusCode"], 500)
        body = json.loads(result["body"])
        self.assertIn("S3からのメール取得に失敗しました", body["message"])
    
    @patch('approve.send_error_notification')
    @patch('approve.extract_and_validate_approval_data')
    @patch('approve.get_email_body_from_s3')
    def test_error_lambda_handler_validation_failure(self, mock_get_email, mock_extract_validate, mock_send_error):
        """異常系: 申請データバリデーション失敗"""
        # テストケース: 申請データのバリデーションでエラー
        # リクエスト: 有効なSESイベント
        # 期待値: APIException(400)、エラー通知送信
        
        # モック設定
        mock_get_email.return_value = "invalid mail body"
        mock_extract_validate.side_effect = APIException(400, "申請理由が見つかりません")
        mock_send_error.return_value = {"success": True}
        
        result = lambda_handler(self.valid_ses_event, self.mock_context)
        
        self.assertEqual(result["statusCode"], 400)
        body = json.loads(result["body"])
        self.assertIn("申請理由が見つかりません", body["message"])
        
        # エラー通知が送信されることを確認
        mock_send_error.assert_called_once()
        error_call_args = mock_send_error.call_args[0]
        self.assertEqual(error_call_args[1], "test@example.com")  # sender_email
        self.assertEqual(error_call_args[2], "テストシステム")    # mail_subject
    
    @patch('approve.send_teams_acceptance_notification')
    @patch('approve.send_teams_approval_message')
    @patch('approve.extract_and_validate_approval_data')
    @patch('approve.get_email_body_from_s3')
    def test_error_lambda_handler_teams_approval_failure(self, mock_get_email, mock_extract_validate, 
                                                       mock_send_approval, mock_send_acceptance):
        """異常系: Teams承認メッセージ送信失敗"""
        # テストケース: Teams承認メッセージ送信でエラー
        # リクエスト: 有効なSESイベント
        # 期待値: APIException(502)
        
        # モック設定
        mock_get_email.return_value = "【申請理由】テスト申請【ログ取得期間】FROM: 2024-01-01 TO: 2024-01-02"
        mock_extract_validate.return_value = ApprovalData(
            mail="test@example.com",
            content="テスト申請",
            system="テストシステム",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 1, 2)
        )
        mock_send_approval.side_effect = APIException(502, "Teams API error")
        
        result = lambda_handler(self.valid_ses_event, self.mock_context)
        
        self.assertEqual(result["statusCode"], 502)
        body = json.loads(result["body"])
        self.assertIn("Teams API error", body["message"])
    
    @patch('approve.send_teams_acceptance_notification')
    @patch('approve.send_teams_approval_message')
    @patch('approve.extract_and_validate_approval_data')
    @patch('approve.get_email_body_from_s3')
    def test_error_lambda_handler_teams_acceptance_failure(self, mock_get_email, mock_extract_validate, 
                                                         mock_send_approval, mock_send_acceptance):
        """異常系: Teams受付通知送信失敗"""
        # テストケース: Teams受付通知送信でエラー
        # リクエスト: 有効なSESイベント
        # 期待値: APIException(502)
        
        # モック設定
        mock_get_email.return_value = "【申請理由】テスト申請【ログ取得期間】FROM: 2024-01-01 TO: 2024-01-02"
        mock_extract_validate.return_value = ApprovalData(
            mail="test@example.com",
            content="テスト申請",
            system="テストシステム",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 1, 2)
        )
        mock_send_approval.return_value = {"success": True}
        mock_send_acceptance.side_effect = APIException(502, "User not found")
        
        result = lambda_handler(self.valid_ses_event, self.mock_context)
        
        self.assertEqual(result["statusCode"], 502)
        body = json.loads(result["body"])
        self.assertIn("User not found", body["message"])
    
    @patch('approve.send_error_notification')
    @patch('approve.get_email_body_from_s3')
    def test_error_lambda_handler_unexpected_exception(self, mock_get_email, mock_send_error):
        """異常系: 想定外の例外発生"""
        # テストケース: 想定外の例外が発生
        # リクエスト: 有効なSESイベント
        # 期待値: システムエラー(500)、エラー通知送信
        
        # モック設定（想定外の例外）
        mock_get_email.side_effect = Exception("Unexpected error")
        mock_send_error.return_value = {"success": True}
        
        result = lambda_handler(self.valid_ses_event, self.mock_context)
        
        self.assertEqual(result["statusCode"], 500)
        body = json.loads(result["body"])
        self.assertIn("システムエラーが発生しました", body["message"])
        self.assertIn("Unexpected error", body["message"])
        
        # エラー通知が送信されることを確認
        mock_send_error.assert_called_once()
    
    def test_error_lambda_handler_missing_environment_variables(self):
        """異常系: 環境変数不足"""
        # テストケース: 必要な環境変数が不足
        # リクエスト: 有効なSESイベント
        # 期待値: APIException(500)
        
        # 環境変数をクリア
        for key in self.env_vars.keys():
            if key in os.environ:
                del os.environ[key]
        
        result = lambda_handler(self.valid_ses_event, self.mock_context)
        
        self.assertEqual(result["statusCode"], 500)
        body = json.loads(result["body"])
        self.assertIn("必要な環境変数が設定されていません", body["message"])


# テスト実行用のメイン関数
if __name__ == '__main__':
    # 特定のテストクラスのみ実行する場合
    # unittest.main(argv=[''], testRunner=unittest.TextTestRunner(verbosity=2), exit=False)
    
    # 全テスト実行
    unittest.main(verbosity=2)
        