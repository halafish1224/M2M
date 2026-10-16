# AIM → Tumblr 自動同步

主版本為 `AIM/index.html`。正式完整頁為 <https://live.itigre.com/AIM/>。
發布目的地固定為 halafish 的既有貼文：
<https://www.tumblr.com/halafish/822847655413792768>。

## 同步內容

每次相關檔案更新到 `main`，GitHub Actions 從最新 `main` 的 HTML 產生 Tumblr 原生 NPF 格式：標題、日期、雙語前言、正文、10 張照片、20 則雙語圖說、雙語註記與 Spotify 播放清單連結。完整互動頁由 GitHub Pages 提供；Tumblr 不注入網站的 CSS、JavaScript、煙火或 Lightbox。Spotify 使用原生連結區塊，完整播放器仍可在主頁使用。

更新前核對部落格、貼文 ID、公開狀態及原創貼文，保存 `before.json`；更新同一篇貼文後重新讀取，核對所有文字、連結、照片順序與替代文字、標籤及網址 slug。圖片網址可能由 Tumblr 轉存；因此圖片回讀核對順序、數量與替代文字，不以 CDN 網址一致作保證。原貼文的發布日期不重新指定，不建立新貼文。

GitHub 是內容主版本，因此在 Tumblr 直接修改的正文會被下一次同步覆蓋；後續文字與照片調整請先修改 `AIM/index.html`。

## 一次性授權設定

目前程式與工作流程已備妥；未取得 API 憑證、未啟用變數前，只會產生預覽，不會向 Tumblr 發出發布請求。

1. 在 [Tumblr Applications](https://www.tumblr.com/oauth/apps) 註冊自己的應用程式，例如 `AIM GitHub Publisher`。網站為 `https://live.itigre.com/AIM/`，用途為從 GitHub 同步自己的 AIM 旅程紀錄。若只使用官方 API Console 取得 OAuth 1.0a 憑證，預設 callback 與未使用的 OAuth2 redirect 可先填 `https://www.tumblr.com/dashboard`；Console 的授權流程會使用它自己的 callback。
2. 到 [Tumblr 官方 API Console](https://api.tumblr.com/console) 使用這個應用程式完成自己的帳號授權，取得 Consumer Key、Consumer Secret、OAuth Token、OAuth Token Secret。若 Console 無法使用，可依 [官方 PyTumblr](https://github.com/tumblr/pytumblr) 的 `interactive_console.py` 完成 OAuth 流程。不要把憑證貼到聊天或提交到程式庫。
3. 在 [GitHub Actions secrets](https://github.com/halafish1224/M2M/settings/secrets/actions) 新增以下四項 Repository secrets：

   - `TUMBLR_CONSUMER_KEY`
   - `TUMBLR_CONSUMER_SECRET`
   - `TUMBLR_OAUTH_TOKEN`
   - `TUMBLR_OAUTH_TOKEN_SECRET`

4. 在 GitHub 的 Actions variables 新增 Repository variable `TUMBLR_SYNC_ENABLED`，值為 `true`。
5. 到 Actions → **Sync AIM to Tumblr** → **Run workflow**，第一次可保留 `dry_run=true` 檢查預覽。實際第一次同步時選 `dry_run=false`。後續符合路徑條件的 `main` 更新會自動同步。
6. 確認工作流程顯示 **Updated and verified**，再打開原貼文核對。只有這一步完成後，才能稱為「自動串接已上線」。

這些憑證屬於帳號授權，可能有超出單篇貼文的 API 能力；程式本身限制為設定檔中的單一既有公開貼文，並禁止轉址。可在 Tumblr 的應用程式設定撤銷憑證，或把 GitHub 變數 `TUMBLR_SYNC_ENABLED` 改為 `false` 停用同步。

## 檢查與復原

不需要安裝第三方 Python 套件。在專案根目錄執行：

```console
python -m unittest discover -s tests -v
python scripts/tumblr_sync.py --dry-run
```

預覽檔位於 `tumblr-preview/post.html`、`post.json`。每次 Actions 執行保留 30 天的 `aim-tumblr-...` artifact，內含預覽，以及實際發布時的公開貼文備份與回讀結果；沒有 API 憑證。遇到 HTTP 錯誤或回讀落差，工作流程會失敗並保留資料供檢查，不會改成另發一篇。

如需復原，先停用同步，再使用該次執行的 `before.json` 中 `content`、`layout`、`tags`、`slug`、`source_url` 對同一篇貼文進行檢查後還原，或透過 Tumblr 編輯器還原內容。不要把整份 API 回應直接當成更新請求送出。

格式與介面依據：[Tumblr API 文件](https://github.com/tumblr/docs/blob/master/api.md)、[NPF 規格](https://github.com/tumblr/docs/blob/master/npf-spec.md)、[官方 Python client 的憑證取得說明](https://github.com/tumblr/pytumblr)。
