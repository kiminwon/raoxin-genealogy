-- 2026-10-21 媳妇节点不再问父母 / 详情卡片不再给「补不了的数字」
--
-- 端上的「还差 N 项」来自服务端响应，端上按 rev 做增量缓存。
-- 只发代码不 bump rev，用户手里那份旧响应会一直挂着那个数字，
-- 直到他自己换一次视图。所以清布局缓存 + rev+1，逼端上重新拉一次。
DELETE FROM tree_layout_cache;
UPDATE family SET data_rev = data_rev + 1;
