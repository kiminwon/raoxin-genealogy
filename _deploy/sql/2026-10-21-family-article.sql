-- 2026-10-21 谱文（编修历史 / 迁徙源流等长文章）
-- 启明拍板：房长及以上直接生效；固定分类；首页家族卡下方入口；纯文本正文。
-- 为什么不用 family 表加字段：文章一篇一篇增删、同分类可多篇（历次修谱各一篇谱序），
-- 一行字段装不下，得是独立的一对多。

CREATE TABLE IF NOT EXISTS `family_article` (
  `id`         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `family_id`  BIGINT UNSIGNED NOT NULL              COMMENT '所属宗族',
  `category`   TINYINT      NOT NULL DEFAULT 6       COMMENT '1谱序 2编修历史 3迁徙源流 4凡例 5字辈派语 6其他',
  `title`      VARCHAR(128) NOT NULL                 COMMENT '标题',
  `content`    MEDIUMTEXT    NOT NULL                COMMENT '正文（纯文本，按空行分段）',
  `sort_order` INT           NOT NULL DEFAULT 0      COMMENT '同分类内排序，小的在前',
  `status`     TINYINT      NOT NULL DEFAULT 1       COMMENT '1正常 0已删除（软删，审计可查）',
  `created_by` BIGINT UNSIGNED NULL                  COMMENT '创建人 user_id',
  `updated_by` BIGINT UNSIGNED NULL                  COMMENT '最后编辑人 user_id',
  `created_at` DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_family_cat` (`family_id`, `category`, `sort_order`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='谱文';
