"""One cached SES client per region / credentials / endpoint (T-5), not one per transactional email."""
from app.core.config import settings
from app.services import invitation_email, ses_client


def test_ses_client_is_built_once_per_configuration(monkeypatch):
    built = []
    monkeypatch.setattr(ses_client.boto3, "client", lambda service, **kw: built.append((service, kw)) or object())
    monkeypatch.delenv("AWS_ENDPOINT_URL_SES", raising=False)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    ses_client.reset_ses_clients()
    try:
        monkeypatch.setattr(settings, "AWS_REGION", "eu-west-1")
        first = ses_client.get_ses_client()
        assert ses_client.get_ses_client() is first
        assert invitation_email._get_ses_client() is first  # invitations share it
        assert len(built) == 1 and built[0][0] == "ses" and built[0][1]["region_name"] == "eu-west-1"

        monkeypatch.setattr(settings, "AWS_REGION", "us-east-1")
        other = ses_client.get_ses_client()
        assert other is not first and len(built) == 2

        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://moto:5000")
        assert ses_client.get_ses_client() is not other and len(built) == 3
    finally:
        ses_client.reset_ses_clients()
