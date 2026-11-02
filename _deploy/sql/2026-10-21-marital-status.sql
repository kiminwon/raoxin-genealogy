-- 2026-10-21 「没有」也是一种回答：未婚 / 暂无子女
--
-- 背景：配偶原本是必填项，没有「未婚」这个出口。用户只能二选一：
--   编一个名字（谱里凭空多出一个不存在的人），或永远卡在 96%。
-- 所以这两个字段记的是**事实**，不是"用户点了跳过"的标记：
--   婚姻状态是家谱本来就该写的一句话（老谱写"未娶""配某氏""继配"）。
--
-- NULL / 0 都表示"还没问过"，会继续出现在待补全清单里。
ALTER TABLE person
  ADD COLUMN marital_status TINYINT NULL
    COMMENT '婚姻状态 1未婚 2已婚 3丧偶 4离异；NULL=未确认' AFTER living_status,
  ADD COLUMN childless TINYINT NOT NULL DEFAULT 0
    COMMENT '1=本人已确认无子女（不再问这一项）' AFTER marital_status;
