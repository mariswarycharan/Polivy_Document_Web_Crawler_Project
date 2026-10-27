"""Content inspection and block classification (offline, synthetic bytes)."""
from crawler_io import classify_block, inspect_html


def test_page_wrapped_in_one_form_keeps_its_text():
    html = ("<html><body><form id='aspnetForm'><input name='__VIEWSTATE' value='x'><main><h1>Bariatric Surgery</h1>"
            "<p>Policy Number: MP-12. Effective Date: 01/01/2024. Medically necessary when ...</p></main></form></body></html>")
    ins = inspect_html(html.encode(), "https://x.com/p", "X")
    assert "Policy Number: MP-12" in ins.text and "__VIEWSTATE" not in ins.text


def test_site_header_and_footer_are_dropped_but_article_header_is_kept():
    html = ("<html><body><header><p>Site banner</p></header><main><article><header><h1>Policy A</h1>"
            "<p>Effective Date: 01/01/2024</p></header><p>Body.</p></article></main><footer>Copyright</footer></body></html>")
    text = inspect_html(html.encode(), "https://x.com/p", "X").text
    assert "Effective Date" in text and "Site banner" not in text and "Copyright" not in text


def test_ordinary_pages_mentioning_captcha_or_access_denied_are_not_walls():
    page = b"<html><body><h1>Contact us</h1><p>This form is protected by reCAPTCHA. Access denied errors: call us.</p></body></html>"
    assert classify_block(200, "https://x.com/c", "https://x.com/c", page) == ""


def test_real_interstitials_and_refusals_are_classified():
    cf = b"<html><head><title>Just a moment...</title></head><body><div id='cf-chl-widget'></div></body></html>"
    assert classify_block(403, "https://x.com/a", "https://x.com/a", cf).startswith("bot_challenge_cloudflare")
    assert classify_block(403, "https://x.com/a", "https://x.com/a", b"<html>Forbidden</html>") == "forbidden_403"
    assert classify_block(401, "https://x.com/a", "https://x.com/a", b"") == "auth_401"
    login = b"<html><body><form><input type='password'></form>Please sign in</body></html>"
    assert classify_block(200, "https://x.com/doc", "https://login.x.com/signin?next=/doc", login) == "login_redirect"


def test_406_and_451_are_refusals():
    assert classify_block(406, "https://x.com/", "https://x.com/", b"") == "refused_406"
    assert classify_block(451, "https://x.com/", "https://x.com/", b"") == "refused_451_legal"


def test_boilerplate_page_headers_are_not_titles_and_labelled_titles_are_read():
    from crawler_text import is_boilerplate_header, is_good_title, title_from_text
    assert is_boilerplate_header("UPMC Health Plan POLICY AND PROCEDURE MANUAL", "UPMC Health Plan")
    assert is_good_title("Policies & Procedures Manual")          # a real HTML page title stays a title
    assert not is_boilerplate_header("Medical Policy Manual")
    assert title_from_text("POLICY AND PROCEDURE MANUAL\nPolicy Name: Dental Implants\nPolicy Number: DP.030") == "Dental Implants"
    assert title_from_text("Policy Number: MP.019\nEffective Date: 01/01/2024") == ""
