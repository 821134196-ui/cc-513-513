"""短信模拟适配器：不真正发送，只落库并打印到控制台。"""
from . import db as store

TEMPLATES = {
    "protection_review": "【铜印房产】{agent} 您好：客户 {client} 预约 {date} 看房（{property}），"
                         "该客户仍在您的保护期内（至 {until}），系统已记录本次带看申请。",
    "viewing_reminder": "【铜印房产】{client} 您好：您已预约 {date} 查看 {property}，请留意带看经纪人 {agent}。",
    "checkin_done": "【铜印房产】{client} 您好：{date} 对 {property} 的带看已签到，感谢确认。",
    "dispute_decided": "【铜印房产】{agent} 您好：佣金归属争议 #{dispute_id} 已由主管裁决，规则版本 {rule_version}，详见系统。",
}


def render(template, vars_):
    base = TEMPLATES.get(template, template)
    try:
        return base.format(**vars_)
    except KeyError:
        return base


def send_sms(conn, phone, template, vars_):
    text = render(template, vars_)
    conn.execute(
        """INSERT INTO sms_log(ts, phone, template, vars_json, status, provider)
           VALUES (?,?,?,?,'queued','mock')""",
        (store.now_iso(), phone or "(无号码)", template,
         __import__("json").dumps(vars_, ensure_ascii=False)),
    )
    print(f"[SMS-MOCK -> {phone}] {text}")
    return text
