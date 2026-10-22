"""Human-readable diagnostics; raw portal errors and URLs stay out of group posts."""

from __future__ import annotations


def diagnose_failure(message: str) -> tuple[str, str]:
    text = message.lower()
    if any(part in text for part in (
        "network.getresponsebody", "navigated away", "罗盘登录/场次接口未就绪",
    )):
        return (
            "罗盘页面导航或登录过渡期间，未取得完整有效的场次接口数据。",
            "打开保留的罗盘标签，确认登录完成；如有验证或权限提示请人工处理，然后重新采集该排班。",
        )
    if any(part in text for part in ("登录", "captcha", "验证码", "滑块")):
        return "登录或人工验证未完成。", "在当前浏览器完成登录/人工验证，确认配置店铺，再重新采集该排班。"
    if any(part in text for part in ("基线", "baseline", "快照", "累计", "数据口径")):
        return "交接快照或累计数据存在缺失/口径问题。", "查看运行记录，核对上一班最终快照、直播间及广告账户，再重试。"
    if any(part in text for part in ("场次", "直播账号", "直播间")):
        return "场次归属或账号身份未能确认。", "查看运行记录，核对排班绑定场次、配置账号及已保存的中断记录，再重试。"
    if any(part in text for part in ("千川", "支付", "卡片")):
        return "千川支付接口或指标卡未取得有效数据。", "检查保留的千川页面登录、权限和全域支付数据；确认后重新采集。"
    if any(part in text for part in ("timeout", "超时", "连接", "connect")):
        return "浏览器/接口连接或页面等待超时。", "确认 Cloak 和目标页面可用，查看失败步骤后重新采集。"
    return "执行发生异常，具体错误已保存在后台运行记录。", "打开后台对应运行记录查看失败步骤，处理后重新采集该排班。"
