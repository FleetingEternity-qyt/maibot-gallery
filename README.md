# 麦麦的图库 (MaiBot Gallery Plugin)

[![GitHub release](https://img.shields.io/github/v/release/FleetingEternity-qyt/maibot-gallery)](https://github.com/FleetingEternity-qyt/maibot-gallery/releases)
[![GitHub issues](https://img.shields.io/github/issues/FleetingEternity-qyt/maibot-gallery)](https://github.com/FleetingEternity-qyt/maibot-gallery/issues)
[![License](https://img.shields.io/github/license/FleetingEternity-qyt/maibot-gallery)](https://github.com/FleetingEternity-qyt/maibot-gallery/blob/main/LICENSE)

## 📖 简介
这是一个为麦麦（MaiBot）开发的“私人相册”插件。
当群友想看某种照片时，麦麦会从自己的图库里随机挑选并发送，仿佛是她自己刚刚拍摄的一样。
支持 VLM（视觉模型）自动识别图片标签，并提供完善的冷却机制防止重复发送同一张图。

## ✨ 核心功能
- **VLM 自动打标**：管理员发图，麦麦自动“看图”，提取标签和口语化描述，无需手动输入。
- **管理员指令纠错**：如果 VLM 识别错了，可以用指令一键修改或删除。
- **群聊拟人化发图**：响应群友的请求，发送对应标签的图片，并附带自然的语气。
- **24 小时冷却机制**：同一张图在 24 小时内不会重复发送，避免过于机械。
- **暗号索图与延迟假象**：图库没图时，后台私聊管理员索要。管理员投喂后，插件会延迟 5-15 秒再发到群里，营造“刚去导照片”的沉浸感。
- **群状态联动**：解析麦麦的群名片（如 `麦麦丨拍日落`），将当前状态注入上下文，让麦麦的发言更贴合人设。

## 🛠️ 安装方法
1. 将本插件放入 MaiBot 的 `plugins/` 目录下。
2. 重启 MaiBot（或通过 WebUI 重载插件）。
3. 在 WebUI 的插件管理页面中，配置 `admin_qq`（管理员 QQ 号）并启用插件。
4. 确保你的 `model_config.toml` 中配置了 `vlm` 任务（用于图片识别）。

## 🎮 使用指南

### 管理员私聊指令
- `/add_image 标签1 标签2`
  发送指令后，在 60 秒内发一张图给麦麦，即可将该图片按指定标签入库。
- `/edit_tag 图片ID 新标签1 新标签2`
  修改某张图片的标签（图片ID在入库成功后的回复中可以找到）。
- `/del_image 标签`
  删除该标签下的所有图片（物理文件也会一并删除）。
- `/gallery_list`
  查看图库当前的图片总数和标签统计。

### 群聊交互（正常聊天即可）
- 群友：“麦麦，看看日落。”
- 麦麦会自动调用工具，发送一张“日落”标签的图片，并配上自然的语气。

## 🔗 仓库地址
https://github.com/FleetingEternity-qyt/maibot-gallery

## 📄 许可证
MIT