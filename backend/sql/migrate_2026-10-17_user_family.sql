-- ============================================================================
--  增量迁移：多族改造（user_family）
--  ---------------------------------------------------------------------------
--  日期   2026-10-17
--  背景   原来 app_user.family_id 一列表示"我属于哪个族"，一人只能有一个族。
--         父系谱和母系谱都要查，所以新增 user_family 记成员关系，
--         app_user 上那三列降级为"当前族的快照"。
--
--  为什么不用 schema.sql：那份是 DROP + CREATE，跑一次等于清库。
--
--  用法（本地）：
--    cd backend
--    .venv\Scripts\python.exe -c "import pymysql,..."   # 或直接用 mysql 客户端
--    mysql -u buddy -p raoxin_genealogy < sql/migrate_2026-10-17_user_family.sql
--
--  幂等：本脚本可以重复执行。
-- ============================================================================

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------------
-- 1. 建表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `user_family` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id`           BIGINT UNSIGNED NOT NULL             COMMENT '账号',
  `family_id`         BIGINT UNSIGNED NOT NULL             COMMENT '所属宗族',
  `role`              TINYINT      NOT NULL DEFAULT 1      COMMENT '本族内角色 1族人 2房长 3族管理员 4平台运营',
  `claimed_person_id` BIGINT UNSIGNED  NULL                COMMENT '本族内认领的本人节点',
  `status`            TINYINT      NOT NULL DEFAULT 1      COMMENT '1正常 2已退出',
  `joined_at`         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `last_active_at`    DATETIME         NULL                COMMENT '最近一次切到本族',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_family` (`user_id`, `family_id`),
  KEY `idx_uf_family_status` (`family_id`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='账号与宗族的成员关系（多族）';


-- ---------------------------------------------------------------------------
-- 2. 回填存量数据
--
--    存量 app_user.role 的语义本来就是"在这个族里的角色"，所以直接搬过去即可，
--    不需要推导。claimed_person_id 同理。
--
--    用 INSERT IGNORE 而不是 NOT EXISTS 子查询：唯一键 uk_user_family 会挡住
--    重复行，脚本重复执行不会插两遍。
-- ---------------------------------------------------------------------------
INSERT IGNORE INTO `user_family`
  (`user_id`, `family_id`, `role`, `claimed_person_id`, `status`, `joined_at`, `last_active_at`)
SELECT
  `id`,
  `family_id`,
  `role`,
  `claimed_person_id`,
  1,
  COALESCE(`created_at`, NOW()),
  COALESCE(`last_login_at`, `updated_at`)
FROM `app_user`
WHERE `family_id` IS NOT NULL;


-- ---------------------------------------------------------------------------
-- 3. 自检：每个有族的账号都该有对应成员行，且角色一致
--    （期望返回 0 行）
-- ---------------------------------------------------------------------------
SELECT u.`id` AS user_id, u.`openid`, u.`family_id`, u.`role` AS app_user_role
FROM `app_user` u
LEFT JOIN `user_family` uf
       ON uf.`user_id` = u.`id` AND uf.`family_id` = u.`family_id`
WHERE u.`family_id` IS NOT NULL
  AND (uf.`id` IS NULL OR uf.`role` <> u.`role`);
