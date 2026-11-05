# PR #160 背景結算排程空窗期失敗事件(2026-11)

## 發生什麼事

合併 PR #160(把「預測 vs 實際」比對從 `/forecast` 頁面現場運算,改成在
`baseline.py` 背景排程裡預先結算)之後,收到 GitHub Actions 寄來的失敗
通知信:「[Bonny-sun/esp32_1] Run failed: forecast - main (7456427)」。
查該次執行(2026-11-05 10:43 UTC,排程自動觸發,非手動)的 log,錯誤是:

```
postgrest.exceptions.APIError: {'message': 'column aqua_forecasts.actual does not exist', 'code': '42703', 'hint': None, 'details': None}
```

## 根因

PR #160 牽涉兩個交付物,但各自的執行時機不同步:

1. **程式碼**(`settle_due_forecasts()` 讀寫 `aqua_forecasts.actual/err/hit`)
   ——PR 合併進 main 後立刻生效,接下來每次 `forecast.yml` 排程
   (每30分鐘一次)觸發就會執行到。
2. **SQL migration**(`sql/09_forecast_eval_columns.sql`,在正式 Supabase
   專案的 `aqua_forecasts` 表加上這三個新欄位)——這個不會隨 PR 合併自動
   執行,要使用者自己到 Supabase SQL Editor 手動貼上去跑一次。

這兩件事中間必然存在一段**空窗期**:只要排程在使用者手動跑完 migration
之前就先自動觸發一次,程式就會去查詢/更新一個資料庫裡根本還不存在的欄
位,直接整個排程工作失敗。

這次的時間序剛好對上:PR 合併 → 10:43 排程自動觸發(此時 migration 還
沒跑)→ 失敗 → 使用者稍後補跑 migration → 10:58 手動重新觸發一次確認
→ 成功。

## 怎麼解決的

- 對照失敗那次執行的時間點跟使用者實際執行 migration 的時間點,確認純
  粹是「migration 還沒跑」這個時間差造成的暫時性失敗,不是程式邏輯有
  問題,不需要改任何程式碼。
- 使用者補跑完 `sql/09_forecast_eval_columns.sql` 後,用 `forecast.yml`
  本來就有的 `workflow_dispatch` 手動觸發一次,確認成功、「已結算筆
  數」開始正常增加。

## 學到的教訓(供下次專案參考)

- **「程式碼」跟「資料庫 schema 變更」分成兩個交付物時,中間一定有空
  窗期**——只要是會被排程自動觸發的背景工作,PR 合併後到使用者真的跑
  完 migration 之前,只要排程剛好觸發就會失敗。下次交付這類改動時,應
  該在說明裡更明確提醒「請先跑完 migration 再讓排程跑」,或讓程式對
  「欄位不存在」這種情況做防禦性處理(例如抓到 `42703` 錯誤碼就印警
  告、跳過這個裝置,而不是讓整個排程工作直接炸掉拖累其他裝置)。
- **收到 GitHub Actions 失敗信,先比對時間點,不要先懷疑程式碼壞了**
  ——很多時候是外部依賴(這次是資料庫 schema)還沒就緒造成的暫時性失
  敗,排查時優先確認「失敗那次執行的時間,跟自己手動操作(例如跑
  migration)的時間點,誰先誰後」,比直接深入除錯程式邏輯更快定位問題。
- **留一個 `workflow_dispatch` 手動觸發入口很值得**——這次能在幾分鐘內
  確認「migration 跑完後就恢復正常」,就是因為 `forecast.yml` 本來就留
  了手動「Run workflow」的按鈕,不用乾等下一次排程時間到才能驗證修復
  有沒有生效。
