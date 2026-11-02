-- 2026-10-21 「暂不记外孙」落库
--
-- 外孙任务（external）原来 skippable 但没有 skipLabel，跳过只是端上本地跳过，
-- 不落库 → 下次进来照旧问，完成度永远上不去（陈启明卡在 0.8 就是这个）。
--
-- 现在答「暂不记外孙」落 external_opt_out=1：记的是一个**决定**
-- （"我这一支的外孙不入谱"），跟「未婚」是同一个道理——老谱上
-- "外孙不列"本身就是一句正经记载，不是"用户不想填"的标记。
ALTER TABLE person
  ADD COLUMN external_opt_out TINYINT NOT NULL DEFAULT 0
  COMMENT '1=已明确「外孙不入谱」（不再追问这位的女儿所出子女）'
  AFTER childless;
