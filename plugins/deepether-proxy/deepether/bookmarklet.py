"""书签小工具（javascript:）生成。

用户在已登录的 chat.deepseek.com 页面上点一下这个书签，token 就会被 POST 到插件的回环端口。
生成逻辑放在这里，便于单测断言语义与安全边界（只读 localStorage、只连 127.0.0.1）。
"""

from typing import Final

from .constants import CREDENTIAL_CLAIM_PATH, LOOPBACK_HOST

_TEMPLATE: Final[str] = (
    "javascript:(async()=>{try{"
    "var raw=localStorage.getItem('userToken');"
    "var tok=raw?((JSON.parse(raw)||{}).value||raw):'';"
    "if(!tok){alert('DeepEther Proxy：没有在本地存储里找到 userToken，请先确认已登录 chat.deepseek.com');return}"
    "var resp=await fetch('__ENDPOINT__',{method:'POST',"
    "headers:{'content-type':'application/json'},"
    "body:JSON.stringify({nonce:__NONCE__,token:tok})});"
    "if(!resp.ok){throw new Error(await resp.text())}"
    "alert('DeepEther Proxy：凭证已送达，可以关闭本页')"
    "}catch(err){"
    "var raw2=localStorage.getItem('userToken');"
    "var tok2=raw2?((JSON.parse(raw2)||{}).value||raw2):'';"
    "prompt('DeepEther Proxy：自动送达失败（'+err+'）。请复制下面的 token，粘贴到插件配置 credential.web_tokens：',tok2)"
    "}})()"
)


def build_bookmarklet(port: int, nonce: str) -> str:
    """生成可直接拖到书签栏的 javascript 书签。

    Args:
        port: 凭证回环服务端口。
        nonce: 当前一次性口令，用于拒绝任意网站的伪造提交。

    Returns:
        str: 单行 `javascript:` 书签代码。
    """

    endpoint = f"http://{LOOPBACK_HOST}:{int(port)}{CREDENTIAL_CLAIM_PATH}"
    escaped_nonce = '"' + str(nonce).replace("\\", "\\\\").replace('"', '\\"') + '"'
    return _TEMPLATE.replace("__ENDPOINT__", endpoint).replace("__NONCE__", escaped_nonce)