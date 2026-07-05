# 米兰《最后的晚餐》周末票监控

这个项目用 GitHub Actions 定时打开**官方 Vivaticket 页面**，监控未来 120 天内所有周六、周日：

- 普通入场票
- 官方固定时段英文讲解票

发现新的可售迹象时，通过 SMTP 发邮件。它**不会自动下单、绕过验证码或占票**；收到邮件后仍需本人打开官方页面完成实名购票和付款。

## 1. 建立仓库

1. 在 GitHub 新建一个 **Private repository**，例如 `last-supper-ticket-watcher`。
2. 将本项目所有文件上传到仓库根目录。
3. 打开仓库的 `Settings → Actions → General`：
   - 在 **Workflow permissions** 选择 **Read and write permissions**。
   - 保存。

写权限仅用于更新 `state.json`，避免同一批票每次运行都重复发邮件。

## 2. 配置邮件 Secrets

进入：

`Settings → Secrets and variables → Actions → New repository secret`

添加以下 Secrets：

| Secret | Gmail 示例 | 说明 |
|---|---|---|
| `SMTP_HOST` | `smtp.gmail.com` | SMTP 服务器 |
| `SMTP_PORT` | `465` | SSL 端口 |
| `SMTP_USERNAME` | `yourname@gmail.com` | 发件账号 |
| `SMTP_PASSWORD` | Gmail App Password | **不要填普通登录密码** |
| `EMAIL_FROM` | `yourname@gmail.com` | 发件地址 |
| `EMAIL_TO` | `yourname@gmail.com` | 收件地址；多个地址用逗号分隔 |

### Gmail App Password

Gmail 通常需要：

1. Google Account 开启两步验证。
2. 搜索并打开 **App passwords**。
3. 新建一个应用密码，例如命名为 `GitHub ticket watcher`。
4. 把生成的 16 位密码放入 `SMTP_PASSWORD`，不要写进代码或 README。

Outlook 常用配置是 `smtp.office365.com`、端口 `587`；代码会自动使用 STARTTLS。

## 3. 第一次测试

1. 打开仓库 `Actions`。
2. 选择 **Check Last Supper tickets**。
3. 点击 **Run workflow**。
4. 勾选 **Send a test email even when no tickets are found**。
5. 运行后检查邮箱。

每次运行都会上传一个诊断附件，其中包含：

- 页面截图
- 渲染后的 HTML
- 页面交互元素摘要
- XHR/fetch 网络响应
- `report.json`

如果网站改版或 GitHub IP 被拦，这些文件能帮助定位问题。

## 4. 默认监控频率

- 苏黎世时间 07:00–22:59：约每 20 分钟检查一次
- 其他时间：约每小时检查一次

GitHub 的定时任务可能延迟几分钟；它不是严格的实时任务。

## 5. 只监控指定周末

在 `.github/workflows/check-tickets.yml` 的 `env` 中加入：

```yaml
TARGET_DATES: "2026-07-18,2026-07-19,2026-07-25,2026-07-26"
```

设置了 `TARGET_DATES` 后，`LOOKAHEAD_DAYS` 和 `WEEKEND_ONLY` 会被忽略。日期格式必须是 `YYYY-MM-DD`。

## 6. 修改人数或范围

工作流中可以修改：

```yaml
MIN_TICKETS: "2"
LOOKAHEAD_DAYS: "120"
WEEKEND_ONLY: "true"
```

注意：票务网站不一定在页面上公开剩余数量，因此邮件表示“可能有票”，不保证打开后仍有两张。

## 7. 防止重复提醒

`state.json` 会保存上一次提醒的日期与票种组合：

- 同一批可售结果不会反复发信。
- 票消失后，状态会清空。
- 同一日期之后重新放票，会再次发信。

## 8. 网站拦截或改版

本项目不尝试破解 CAPTCHA 或绕过反机器人措施。若诊断截图显示验证码、Access Denied，GitHub 托管 runner 可能无法稳定访问；这时可以把同一 workflow 改到自己家里的 self-hosted runner，但仍应保持合理检查频率并遵守票务网站规则。
