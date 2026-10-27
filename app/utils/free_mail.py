# app/utils/free_mail.py
"""
Free / consumer mail providers (BR-CM-03/04): an address at one of these domains belongs to a
person, not to a company, so no company is created or matched from it.

The check is on the whole domain (mail.acme.com is a company, orange.fr is a consumer ISP), with
one extra rule: for the big brands below every regional domain is consumer mail
(yahoo.co.jp, hotmail.it, outlook.com.br, ...), so the brand label in front of a public suffix
counts too.
"""

from typing import Optional

from app.utils.company_domain import public_suffix_length, split_domain

FREE_MAIL_DOMAINS = frozenset({
    # Google, Microsoft, Yahoo, Apple, AOL
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "passport.com",
    "yahoo.com", "ymail.com", "rocketmail.com", "icloud.com", "me.com", "mac.com", "aol.com", "aim.com",
    # Privacy / independent providers
    "protonmail.com", "protonmail.ch", "proton.me", "pm.me", "tutanota.com", "tutanota.de", "tuta.io",
    "tutamail.com", "fastmail.com", "fastmail.fm", "hushmail.com", "hey.com", "mailfence.com",
    "posteo.de", "posteo.net", "runbox.com", "disroot.org", "zoho.com", "zohomail.com", "zohomail.in",
    "mail.com", "email.com", "usa.com", "inbox.com", "lycos.com", "gmx.com", "gmx.net", "gmx.de", "gmx.at",
    "gmx.ch", "web.de", "freenet.de", "t-online.de", "arcor.de",
    # Europe: ISPs and national portals
    "orange.fr", "wanadoo.fr", "free.fr", "laposte.net", "sfr.fr", "neuf.fr", "bbox.fr", "aliceadsl.fr",
    "libero.it", "virgilio.it", "tin.it", "alice.it", "tiscali.it", "email.it", "telefonica.net",
    "terra.es", "ziggo.nl", "kpnmail.nl", "home.nl", "planet.nl", "hetnet.nl", "telenet.be", "skynet.be",
    "bluewin.ch", "seznam.cz", "centrum.cz", "wp.pl", "o2.pl", "onet.pl", "interia.pl", "op.pl",
    "btinternet.com", "btopenworld.com", "sky.com", "virginmedia.com", "ntlworld.com", "blueyonder.co.uk",
    "talktalk.net", "tiscali.co.uk", "mail.ru", "inbox.ru", "list.ru", "bk.ru", "rambler.ru",
    "yandex.com", "yandex.ru", "ukr.net",
    # North America
    "att.net", "sbcglobal.net", "bellsouth.net", "comcast.net", "xfinity.com", "verizon.net", "cox.net",
    "charter.net", "spectrum.net", "earthlink.net", "optonline.net", "frontier.com", "windstream.net",
    "juno.com", "netzero.net", "netzero.com", "rogers.com", "shaw.ca", "sympatico.ca", "telus.net",
    "videotron.ca",
    # Asia-Pacific, Latin America
    "rediffmail.com", "qq.com", "foxmail.com", "163.com", "126.com", "yeah.net", "sina.com", "sina.cn",
    "sohu.com", "aliyun.com", "naver.com", "daum.net", "hanmail.net", "nate.com", "bigpond.com",
    "bigpond.net.au", "optusnet.com.au", "iinet.net.au", "xtra.co.nz", "uol.com.br", "bol.com.br",
    "terra.com.br", "ig.com.br",
})

# Brands whose every regional domain is consumer mail: <brand>.<public suffix>
FREE_MAIL_BRANDS = frozenset({
    "gmail", "googlemail", "yahoo", "ymail", "hotmail", "outlook", "live", "msn", "windowslive",
    "aol", "gmx", "yandex", "protonmail", "zohomail", "rediffmail",
})


def is_free_mail_domain(domain: Optional[str]) -> bool:
    """True for a consumer mail domain (gmail.com, orange.fr, yahoo.co.jp); False for blank input."""
    labels = split_domain(domain)
    if not labels:
        return False
    full = ".".join(labels)
    if full in FREE_MAIL_DOMAINS:
        return True
    suffix = public_suffix_length(labels)
    # Subdomains of a provider (e.g. "us.att.net"): look at the registrable part too
    registrable = labels[-(suffix + 1):] if len(labels) > suffix else labels
    if ".".join(registrable) in FREE_MAIL_DOMAINS:
        return True
    return len(labels) > suffix and registrable[0] in FREE_MAIL_BRANDS
