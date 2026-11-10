"""自签名 TLS 证书生成（V2.2 追加层）。

用途：手机浏览器只有在**安全上下文**（HTTPS / localhost）才暴露
``navigator.mediaDevices``。安卓 Chrome 打开 ``http://<LAN_IP>:<PORT>/`` 时
``navigator.mediaDevices`` 是 ``undefined``，前端连「请求麦克风权限」的调用
都发不出去 —— 这不是用户拒绝，而是浏览器根本没给 API。因此语音功能必须
走 HTTPS。

设计取舍（为什么自签而不是 CA 证书）：

* 局域网工具不持有公网域名，无法申请正规证书；
* 自签名后手机首次访问会弹「连接不是私密连接」，点「高级 → 继续前往」后
  页面即为 https 源，``isSecureContext === true``，麦克风权限弹窗正常出现；
* 证书落盘复用（``server/certs/*.pem``），同一台机器下次启动不再变化，
  手机端的「继续前往」只需点一次（Chrome 按证书指纹记忆例外）。

实现要点：

* 用 ``cryptography`` 直接生成，不依赖 openssl 命令行；
* SAN 必须包含 IP 与主机名 —— 现代浏览器不再看 CN 字段，只看 SAN；
* 私钥文件权限尽量收紧（POSIX chmod 600），Windows 上退化为默认 ACL；
* 任何失败都不抛到服务层：返回 ``(None, 原因)``，调用方据此回退 HTTP。
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import socket
from pathlib import Path

CERT_DIR = Path(__file__).resolve().parent / "certs"
CERT_FILE = CERT_DIR / "lan-cert.pem"
KEY_FILE = CERT_DIR / "lan-key.pem"

# 证书有效期：局域网自用，给足 10 年，避免到期后手机突然连不上。
_VALID_DAYS = 3650


class CertError(RuntimeError):
    """证书生成/加载失败。"""


def _load_cryptography():
    """延迟导入，缺依赖时给出可读原因（服务仍能用 HTTP 跑起来）。"""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except Exception as exc:  # pragma: no cover - 环境缺依赖时走这里
        raise CertError(f"缺少 cryptography 依赖：{exc}") from exc
    return x509, hashes, serialization, rsa, NameOID


def _san_entries(lan_ip: str) -> list:
    """构造 SAN：局域网 IP + 本机回环 + 主机名。"""
    x509, _hashes, _ser, _rsa, _oid = _load_cryptography()
    entries: list = []
    for candidate in (lan_ip, "127.0.0.1"):
        text = str(candidate or "").strip()
        if not text:
            continue
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(text)))
        except ValueError:
            continue
    names = {"localhost"}
    try:
        host = socket.gethostname()
        if host:
            names.add(host)
    except Exception:
        pass
    for name in sorted(names):
        entries.append(x509.DNSName(name))
    return entries


def generate(lan_ip: str, force: bool = False) -> tuple[Path, Path]:
    """生成（或复用）自签名证书，返回 ``(cert_path, key_path)``。

    ``force=True`` 时覆盖已有文件（换机 / 换 IP 后重新生成）。
    """
    if not force and CERT_FILE.exists() and KEY_FILE.exists():
        return CERT_FILE, KEY_FILE

    x509, hashes, serialization, rsa, NameOID = _load_cryptography()

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, f"Remote Control ({lan_ip})"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "LAN Remote Control"),
        ]
    )
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        .not_valid_after(now + _dt.timedelta(days=_VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(_san_entries(lan_ip)), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        # 允许浏览器把这张证书当作服务端证书使用（现代浏览器需要 EKU）。
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    try:
        CERT_DIR.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        try:
            KEY_FILE.chmod(0o600)
        except Exception:
            pass
    except Exception as exc:
        raise CertError(f"写入证书失败：{exc}") from exc

    return CERT_FILE, KEY_FILE


def build_ssl_context(lan_ip: str, force: bool = False):
    """构造 ``ssl.SSLContext``；失败抛出 :class:`CertError`。"""
    import ssl

    cert_path, key_path = generate(lan_ip, force=force)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # 只允许 TLS1.2+：安卓 Chrome 与 iOS Safari 都满足，老协议没必要留。
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    except Exception as exc:
        raise CertError(f"加载证书失败：{exc}") from exc
    return context


def ensure_for_ip(lan_ip: str) -> tuple[bool, str]:
    """检查现有证书是否覆盖当前 IP；不覆盖则重新生成。

    返回 ``(ok, 说明)``，永不抛异常，方便启动日志直说结果。
    """
    try:
        if CERT_FILE.exists() and KEY_FILE.exists():
            try:
                from cryptography import x509

                data = x509.load_pem_x509_certificate(CERT_FILE.read_bytes())
                san = data.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                if ipaddress.ip_address(str(lan_ip)) in san.get_values_for_type(x509.IPAddress):
                    return True, "复用已有证书"
            except Exception:
                pass
        generate(lan_ip, force=True)
        return True, "已生成新证书"
    except CertError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
