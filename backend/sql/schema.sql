-- ============================================================================
--  饶信谱脉 · 家谱小程序  ——  数据库建表脚本
--  ---------------------------------------------------------------------------
--  版本      V1.0
--  日期      2026-09-24
--  目标库    MySQL 8.0+ / InnoDB / utf8mb4
--  字符集    若使用 MySQL 5.7，请把 utf8mb4_0900_ai_ci 全部替换为 utf8mb4_unicode_ci
--
--  【命名说明】
--    family_union  设计概念叫 "Union（婚姻单元）"，物理表名避开 SQL 保留字 UNION
--    app_user      避开 mysql.user 系统表与 USER() 函数
--
--  【重要约定】
--    1. 不使用物理外键（FOREIGN KEY），只建索引，一致性由应用层保证 + 定期巡检。
--       原因见《家谱小程序-数据库设计.md》第五节。
--    2. 一律软删除：status = 0。不做物理 DELETE。
--    3. 关系建模必须走 family_union + parent_child 两层，
--       严禁在 person 上增加 father_id / mother_id 字段。
--    4. 所有 person 数据对外输出前必须经过 API 层脱敏投影函数。
-- ============================================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ============================================================================
-- 1. family  宗族（一棵家族树 = 一条记录）
-- ============================================================================
DROP TABLE IF EXISTS `family`;
CREATE TABLE `family` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

  -- ---- 标识信息 ----
  `surname`           VARCHAR(16)  NOT NULL                COMMENT '主姓氏，如 陈',
  `name`              VARCHAR(64)  NOT NULL                COMMENT '宗族名，如 广信陈氏',
  `tang_hao`          VARCHAR(32)      NULL                COMMENT '堂号，如 颍川堂',
  `jun_wang`          VARCHAR(32)      NULL                COMMENT '郡望，如 颍川',
  `origin_place`      VARCHAR(128)     NULL                COMMENT '始迁地，如 福建建阳',
  `settle_place`      VARCHAR(128)     NULL                COMMENT '落籍地，如 上饶市广信区茶亭镇',
  `founder_name`      VARCHAR(64)      NULL                COMMENT '始迁祖名讳',
  `founder_era`       VARCHAR(64)      NULL                COMMENT '始迁年代，如 明永乐年间',

  -- ---- 字辈诗：决策第1条「只做提示参考，不做强制校验」 ----
  -- generation_poem 第 i 个汉字对应第 (first_gen_label + i) 世
  -- 例：poem='文章华国诗礼传家', first_gen_label=15  -> '国' 字对应第 18 世
  `generation_poem`   VARCHAR(512)     NULL                COMMENT '字辈诗，连续字符串，如 文章华国诗礼传家',
  `generation_poem_note` VARCHAR(512)  NULL                COMMENT '字辈诗说明：起于哪一世、是否循环、男女是否通用',
  `first_gen_label`   INT          NOT NULL DEFAULT 1      COMMENT '字辈诗首字对应的世次',
  `gen_poem_enabled`  TINYINT      NOT NULL DEFAULT 1      COMMENT '字辈提示开关：1开启 0关闭（族管理员可关）',

  -- ---- 树配置 ----
  `show_external_in_overview` TINYINT NOT NULL DEFAULT 0   COMMENT '全族总览是否显示外姓姻亲(role4/5)：0否(默认) 1是',

  -- ---- 访问入口：决策第5条「浏览码」 ----
  `view_code`         CHAR(8)          NULL                COMMENT '全族浏览码（只看不改），8位短码',
  `view_code_enabled` TINYINT      NOT NULL DEFAULT 1      COMMENT '浏览码是否有效',

  -- ---- 版本号：用于树布局缓存失效 ----
  `data_rev`          BIGINT       NOT NULL DEFAULT 0      COMMENT '数据版本号，任何落库变更 +1',

  `status`            TINYINT      NOT NULL DEFAULT 1      COMMENT '1正常 0停用',
  `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_view_code` (`view_code`),
  KEY `idx_surname` (`surname`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='宗族（一棵树）';


-- ============================================================================
-- 2. branch  支系 / 房
-- ============================================================================
DROP TABLE IF EXISTS `branch`;
CREATE TABLE `branch` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `family_id`         BIGINT UNSIGNED NOT NULL             COMMENT '所属宗族',
  `name`              VARCHAR(64)  NOT NULL                COMMENT '房名，如 长房 / 三房 / 东头屋里',
  `founder_person_id` BIGINT UNSIGNED  NULL                COMMENT '房始祖 person_id（与 person 循环引用，故不建外键）',
  `sort_no`           INT          NOT NULL DEFAULT 0      COMMENT '展示排序',
  `status`            TINYINT      NOT NULL DEFAULT 1      COMMENT '1正常 0停用',
  `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_family` (`family_id`, `sort_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='支系（房）';


-- ============================================================================
-- 3. person  人物
--    【严禁】在此表添加 father_id / mother_id，关系走 family_union + parent_child
-- ============================================================================
DROP TABLE IF EXISTS `person`;
CREATE TABLE `person` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `family_id`         BIGINT UNSIGNED NOT NULL             COMMENT '主族',
  `branch_id`         BIGINT UNSIGNED  NULL                COMMENT '所属房',
  `generation`        INT              NULL                COMMENT '世次（以始迁祖为第1世），外姓人仅作定位用',

  -- ---- 字辈校验结果：决策第1条「只提示不强制」 ----
  -- 仅在 lineage_role=1 且 generation_name 非空时计算；用户可无视提示照常提交
  `generation_hint`   TINYINT      NOT NULL DEFAULT 0      COMMENT '字辈校验：0未校验 1匹配 2不匹配(仅提示) 3用户已忽略',
  `generation_name`   VARCHAR(16)      NULL                COMMENT '本人在谱名中使用的字辈用字',

  -- ---- 谱系角色：决定该人出现在哪张视图 ----
  `lineage_role`      TINYINT      NOT NULL DEFAULT 1      COMMENT '1本族男丁 2本族女儿 3媳妇 4女婿 5外姓后代(外孙及子孙)',
  `is_lineage_carrier` TINYINT     NOT NULL DEFAULT 0      COMMENT '是否承袭世系（全族树纵向主链是否从此人往下走）',

  -- ---- 姓名：name 可空是刻意设计，见设计文档 3.2 ----
  `name`              VARCHAR(64)      NULL                COMMENT '姓名全称，可空（老谱"配王氏"场景）',
  `surname`           VARCHAR(16)      NULL                COMMENT '姓氏；name 有值时由 name 首字派生，name 为空时为独立信息',
  `name_pinyin`       VARCHAR(64)      NULL                COMMENT '拼音（检索用）',
  `gender`            TINYINT      NOT NULL DEFAULT 0      COMMENT '1男 2女 0未知',
  `courtesy_name`     VARCHAR(64)      NULL                COMMENT '字',
  `art_name`          VARCHAR(64)      NULL                COMMENT '号',
  `rank_in_family`    VARCHAR(32)      NULL                COMMENT '排行，如 行三 / 长子',
  `name_remark`       VARCHAR(128)     NULL                COMMENT '名讳说明，如 讳国栋，谱名国栋',

  -- ---- 生卒葬 ----
  `birth_date`        DATE             NULL                COMMENT '出生日期（精确到哪一层由 precision 决定）',
  `birth_date_precision` TINYINT   NOT NULL DEFAULT 0      COMMENT '0未知 1仅年 2年月 3年月日',
  `death_date`        DATE             NULL                COMMENT '去世日期',
  `death_date_precision` TINYINT   NOT NULL DEFAULT 3      COMMENT '同上',
  `living_status`     TINYINT      NOT NULL DEFAULT 2      COMMENT '1在世 2已故 3不详',
  `marital_status`    TINYINT          NULL                COMMENT '婚姻状态 1未婚 2已婚 3丧偶 4离异；NULL=未确认（会继续问配偶）',
  `childless`         TINYINT      NOT NULL DEFAULT 0      COMMENT '1=本人已确认无子女（不再问这一项）',
  `external_opt_out`  TINYINT      NOT NULL DEFAULT 0      COMMENT '1=已明确「外孙不入谱」（不再追问这位的女儿所出子女）',
  `burial_place`      VARCHAR(255)     NULL                COMMENT '葬地',
  `burial_note`       VARCHAR(512)     NULL                COMMENT '葬向 / 碑记 / 墓志',

  -- ---- 社会信息（在世者受脱敏规则约束） ----
  `education`         VARCHAR(64)      NULL                COMMENT '学历 / 功名（老谱多为"邑庠生""国学生"）',
  `occupation`        VARCHAR(64)      NULL                COMMENT '职业',
  `biography`         TEXT             NULL                COMMENT '生平小传',
  `native_place`      VARCHAR(128)     NULL                COMMENT '籍贯 / 居住地，【仅到乡镇级，严禁填写详细住址】',

  -- ---- 史料来源 ----
  `source_type`       TINYINT      NOT NULL DEFAULT 1      COMMENT '1老谱录入 2族人填报 3OCR导入 4口述 5AI推断',
  `source_note`       VARCHAR(255)     NULL                COMMENT '来源备注，如 光绪十八年谱卷三第十二页',
  `confidence`        TINYINT      NOT NULL DEFAULT 2      COMMENT '可信度：1高 2中 3待考',
  `verify_status`     TINYINT      NOT NULL DEFAULT 1      COMMENT '核验状态：1未核 2已核 3存疑',

  -- ---- 隐私（脱敏在 API 层执行） ----
  `privacy_level`     TINYINT      NOT NULL DEFAULT 2      COMMENT '1公开 2族人可见 3近亲可见 4私密(仅本人+管理员)',

  -- ---- 生命周期 ----
  `status`            TINYINT      NOT NULL DEFAULT 1      COMMENT '1正常 0已删（软删）',
  `merged_into_id`    BIGINT UNSIGNED  NULL                COMMENT '被合并到哪个 person（软合并，保留追溯）',

  -- ---- 预留：决策第3条「首期不做跨树，但预留字段」 ----
  -- 将来做母系时，升级为中间表 person_family_membership(person_id, family_id, relation_type, is_primary)
  `alt_family_id`     BIGINT UNSIGNED  NULL                COMMENT '【预留】跨族关联：该人在母系/外姓族谱中的 family_id',

  `created_by`        BIGINT UNSIGNED  NULL                COMMENT '创建者 app_user.id（0 表示系统导入）',
  `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (`id`),
  KEY `idx_family_gen` (`family_id`, `generation`),
  KEY `idx_family_role` (`family_id`, `lineage_role`),
  KEY `idx_branch` (`branch_id`),
  KEY `idx_family_name` (`family_id`, `name`),
  KEY `idx_surname_name` (`surname`, `name`),
  KEY `idx_status` (`status`),
  KEY `idx_merged` (`merged_into_id`),
  KEY `idx_alt_family` (`alt_family_id`),
  KEY `idx_living` (`family_id`, `living_status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='人物';


-- ============================================================================
-- 4. family_union  婚姻单元
--    partner_a_id 定义为「承袭世系的一方」（通常父方；招赘时为女儿本人）
--    回溯父亲：child -> parent_child(is_primary=1) -> family_union -> partner_a_id
--    回溯母亲：同一 union 的 partner_b_id
-- ============================================================================
DROP TABLE IF EXISTS `family_union`;
CREATE TABLE `family_union` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `family_id`         BIGINT UNSIGNED NOT NULL             COMMENT '所属宗族',
  `partner_a_id`      BIGINT UNSIGNED NOT NULL             COMMENT '伴侣A = 承袭世系的一方',
  `partner_b_id`      BIGINT UNSIGNED  NULL                COMMENT '伴侣B = 配偶方；NULL 表示"配某氏"信息不详 / 未婚生育',
  `union_type`        TINYINT      NOT NULL DEFAULT 1      COMMENT '1初婚 2再婚 3纳妾(第二配偶) 4入赘 5招赘 6未婚生育 7不详',
  `union_seq`         TINYINT      NOT NULL DEFAULT 1      COMMENT '同类婚姻中的第几段',
  `start_date`        DATE             NULL                COMMENT '成婚日期',
  `start_date_precision` TINYINT   NOT NULL DEFAULT 0      COMMENT '0未知 1年 2年月 3年月日',
  `end_date`          DATE             NULL                COMMENT '结束日期（丧偶/离异）',
  `end_reason`        TINYINT          NULL                COMMENT '1丧偶 2离异 3不详',
  `remark`            VARCHAR(255)     NULL                COMMENT '备注，如 侧室，无出',
  `status`            TINYINT      NOT NULL DEFAULT 1      COMMENT '1正常 0已删',
  `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_family` (`family_id`),
  KEY `idx_partner_a` (`partner_a_id`),
  KEY `idx_partner_b` (`partner_b_id`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='婚姻单元';


-- ============================================================================
-- 5. parent_child  亲子关系
--    过继场景：同一 child 挂两条记录
--      生父的 union  -> relation=1(亲生), is_primary=0, is_biological=1
--      嗣父的 union  -> relation=2(过继), is_primary=1, is_biological=0
-- ============================================================================
DROP TABLE IF EXISTS `parent_child`;
CREATE TABLE `parent_child` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `family_id`         BIGINT UNSIGNED NOT NULL             COMMENT '所属宗族',
  `union_id`          BIGINT UNSIGNED NOT NULL             COMMENT '所属婚姻单元',
  `child_id`          BIGINT UNSIGNED NOT NULL             COMMENT '子女 person_id',
  `relation`          TINYINT      NOT NULL DEFAULT 1      COMMENT '1亲生 2过继(嗣) 3收养 4继子女 5随母 6不详',
  `is_primary`        TINYINT      NOT NULL DEFAULT 1      COMMENT '是否主谱关系：决定此人显示在谁下面、算哪一世',
  `is_biological`     TINYINT      NOT NULL DEFAULT 1      COMMENT '该 union 双方是否为生父母（过继到嗣父时=0）',
  `seq`               SMALLINT     NOT NULL DEFAULT 0      COMMENT '同父母子女中的排行序号',
  `remark`            VARCHAR(255)     NULL                COMMENT '备注，如 出继三房文海公为嗣',
  `status`            TINYINT      NOT NULL DEFAULT 1      COMMENT '1正常 0已删',
  `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_union_child_rel` (`union_id`, `child_id`, `relation`),
  KEY `idx_child` (`child_id`, `is_primary`),
  KEY `idx_family` (`family_id`),
  KEY `idx_union` (`union_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='亲子关系';


-- ============================================================================
-- 6. app_user  微信用户（账号，非家谱数据）
-- ============================================================================
DROP TABLE IF EXISTS `app_user`;
CREATE TABLE `app_user` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `openid`            VARCHAR(64)  NOT NULL                COMMENT '微信 openid',
  `unionid`           VARCHAR(64)      NULL                COMMENT '微信 unionid',
  `nickname`          VARCHAR(64)      NULL                COMMENT '微信昵称',
  `avatar_url`        VARCHAR(512)     NULL                COMMENT '头像地址',
  `real_name`         VARCHAR(32)      NULL                COMMENT '用户自填真实姓名（不写入家谱数据）',

  -- 账号级身份核验信息，【不进入家谱数据、不对外展示、不参与任何家谱查询】
  -- 若不使用微信手机号核验，可删除本字段，认领审核改为纯人工线下核实
  `phone_enc`         VARBINARY(255)   NULL                COMMENT '手机号密文（仅账号核验用）',

  `role`              TINYINT      NOT NULL DEFAULT 1      COMMENT '1普通用户 2房长 3族管理员 4平台运营',
  `family_id`         BIGINT UNSIGNED  NULL                COMMENT '主族',
  `claimed_person_id` BIGINT UNSIGNED  NULL                COMMENT '已认领的本人节点 person_id',
  `status`            TINYINT      NOT NULL DEFAULT 1      COMMENT '1正常 0禁用',
  `last_login_at`     DATETIME         NULL,
  `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_openid` (`openid`),
  KEY `idx_family_role` (`family_id`, `role`),
  KEY `idx_claimed` (`claimed_person_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='微信用户账号';


-- ============================================================================
-- 7. user_family  账号与宗族的成员关系（多族）
--    父系谱和母系谱都要查，app_user.family_id 一列装不下。
--    app_user.family_id / role / claimed_person_id 降级为"当前族的快照"，
--    切族时由应用层 app/services/family_member.py::activate() 回填。
--    鉴权代码只读快照，所以加多族不用改鉴权。
-- ============================================================================
DROP TABLE IF EXISTS `user_family`;
CREATE TABLE `user_family` (
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


-- ============================================================================
-- 8. claim  认领申请
-- ============================================================================
DROP TABLE IF EXISTS `claim`;
CREATE TABLE `claim` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `family_id`         BIGINT UNSIGNED NOT NULL             COMMENT '所属宗族',
  `user_id`           BIGINT UNSIGNED NOT NULL             COMMENT '申请用户',
  `person_id`         BIGINT UNSIGNED NOT NULL             COMMENT '申请认领的 person_id',
  `claim_type`        TINYINT      NOT NULL DEFAULT 1      COMMENT '1新建本人节点 2认领已有节点',
  `evidence`          VARCHAR(512)     NULL                COMMENT '佐证自述，如 我父亲是陈国栋，我生于1988年',
  `evidence_files`    JSON             NULL                COMMENT '佐证材料（图片 URL 数组）',
  `verify_method`     TINYINT      NOT NULL DEFAULT 1      COMMENT '1答题自证 2房长审核 3邀请码',
  `verify_answers`    JSON             NULL                COMMENT '答案快照（含命中节点与证据强度），供申诉/追溯/审计',
  `status`            TINYINT      NOT NULL DEFAULT 1      COMMENT '1待审 2通过 3驳回 4撤回 5冲突(多人认领同一节点)',
  `reviewer_user_id`  BIGINT UNSIGNED  NULL                COMMENT '审核人',
  `review_note`       VARCHAR(255)     NULL                COMMENT '审核意见',
  `reviewed_at`       DATETIME         NULL,
  `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_person_status` (`person_id`, `status`),
  KEY `idx_user` (`user_id`),
  KEY `idx_family_status` (`family_id`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='认领申请';


-- ============================================================================
-- 9. invite_code  邀请码（决策第5条：方案 C，两种码）
--    type=1 浏览码：族级，只看不改，用于家族群大规模扩散
--    type=2 编辑码：节点级，锚定 anchor_person_id，可编辑 scope 范围内节点
-- ============================================================================
DROP TABLE IF EXISTS `invite_code`;
CREATE TABLE `invite_code` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `family_id`         BIGINT UNSIGNED NOT NULL             COMMENT '所属宗族',
  `code`              CHAR(8)      NOT NULL                COMMENT '8位短码，URL 安全字符集',
  `code_type`         TINYINT      NOT NULL DEFAULT 1      COMMENT '1浏览码(只看不改) 2编辑码(可改指定范围)',

  -- ---- 编辑码专属（type=2） ----
  `anchor_person_id`  BIGINT UNSIGNED  NULL                COMMENT '锚定节点，通常是邀请人自己',
  `anchor_user_id`    BIGINT UNSIGNED  NULL                COMMENT '生成者 app_user.id',
  `scope_rule`        JSON             NULL                COMMENT '权限规则，如 {"up":3,"down":2,"spouse":true,"siblings":false,"externalDesc":2}',
  `scope_person_ids`  JSON             NULL                COMMENT '【签发时快照】按规则解析出的可编辑 person_id 数组。鉴权以快照为准，防止权限随树变动漂移',
  `scope_node_count`  INT          NOT NULL DEFAULT 0      COMMENT '快照节点数，用于展示"这个链接能改 N 个节点"',

  `inviter_user_id`   BIGINT UNSIGNED  NULL                COMMENT '邀请人（浏览码为族管理员）',
  `invite_note`       VARCHAR(128)     NULL                COMMENT '给谁用的备注，如 给二叔',
  `max_uses`          INT          NOT NULL DEFAULT 0      COMMENT '0=不限次数',
  `used_count`        INT          NOT NULL DEFAULT 0      COMMENT '已使用次数',
  `expires_at`        DATETIME         NULL                COMMENT '过期时间，NULL=长期有效',
  `status`            TINYINT      NOT NULL DEFAULT 1      COMMENT '1有效 0已失效(手动吊销或过期)',
  `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_code` (`code`),
  KEY `idx_family_type` (`family_id`, `code_type`),
  KEY `idx_anchor` (`anchor_person_id`),
  KEY `idx_inviter` (`inviter_user_id`),
  KEY `idx_expires` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='邀请码（浏览码/编辑码）';


-- ============================================================================
-- 10. edit_scope  可编辑范围（鉴权主查询表）
--    来源：认领通过 / 编辑码被接受 / 房长授权
-- ============================================================================
DROP TABLE IF EXISTS `edit_scope`;
CREATE TABLE `edit_scope` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id`           BIGINT UNSIGNED NOT NULL             COMMENT '授权的用户',
  `family_id`         BIGINT UNSIGNED NOT NULL             COMMENT '所属宗族',
  `person_id`         BIGINT UNSIGNED NOT NULL             COMMENT '被授权可编辑的节点',
  `source`            TINYINT      NOT NULL DEFAULT 1      COMMENT '1认领自己 2邀请码 3房长授权 4管理员',
  `invite_code_id`    BIGINT UNSIGNED  NULL                COMMENT '来源邀请码',
  `granted_by`        BIGINT UNSIGNED  NULL                COMMENT '授权人 user_id',
  `granted_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `expires_at`        DATETIME         NULL                COMMENT 'NULL=长期有效',
  `status`            TINYINT      NOT NULL DEFAULT 1      COMMENT '1有效 0已回收',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_person` (`user_id`, `person_id`),
  KEY `idx_person` (`person_id`),
  KEY `idx_user_family` (`user_id`, `family_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户可编辑范围';


-- ============================================================================
-- 11. revision  修订记录（V1 采用"先审后发"）
--     所有写操作先进入本表，管理员通过后才落到 person / family_union / parent_child
-- ============================================================================
DROP TABLE IF EXISTS `revision`;
CREATE TABLE `revision` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `family_id`         BIGINT UNSIGNED NOT NULL             COMMENT '所属宗族',
  `target_table`      VARCHAR(32)  NOT NULL                COMMENT 'person / family_union / parent_child',
  `target_id`         BIGINT UNSIGNED  NULL                COMMENT 'NULL 表示新增',
  `op`                TINYINT      NOT NULL DEFAULT 2      COMMENT '1新增 2修改 3删除',
  `payload`           JSON         NOT NULL                COMMENT '变更后的字段值',
  `before_snapshot`   JSON             NULL                COMMENT '变更前快照，用于回滚',
  `diff_summary`      VARCHAR(512)     NULL                COMMENT '人类可读摘要，如 修改 陈国栋 的出生日期：1965 → 1966',
  `submitter_user_id` BIGINT UNSIGNED NOT NULL             COMMENT '提交人',
  `submitter_person_id` BIGINT UNSIGNED NULL               COMMENT '提交人对应的人物节点',
  `submit_note`       VARCHAR(255)     NULL                COMMENT '提交说明',
  `status`            TINYINT      NOT NULL DEFAULT 1      COMMENT '1待审 2已通过 3已驳回 4自动合并 5撤回',
  `conflict_with_id`  BIGINT UNSIGNED  NULL                COMMENT '与另一条修订冲突时互指',
  `reviewer_user_id`  BIGINT UNSIGNED  NULL,
  `review_note`       VARCHAR(255)     NULL,
  `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `reviewed_at`       DATETIME         NULL,
  PRIMARY KEY (`id`),
  KEY `idx_target` (`target_table`, `target_id`),
  KEY `idx_family_status` (`family_id`, `status`, `created_at`),
  KEY `idx_submitter` (`submitter_user_id`, `status`),
  KEY `idx_conflict` (`conflict_with_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='修订记录（待审队列）';


-- ============================================================================
-- 12. tree_layout_cache  家族树布局缓存
--     缓存是否有效由 family_rev 与 family.data_rev 比对决定
-- ============================================================================
DROP TABLE IF EXISTS `tree_layout_cache`;
CREATE TABLE `tree_layout_cache` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `family_id`         BIGINT UNSIGNED NOT NULL             COMMENT '所属宗族',
  `view_type`         TINYINT      NOT NULL                COMMENT '1我的一家 2我的支系 3全族总览',
  `anchor_person_id`  BIGINT UNSIGNED  NULL                COMMENT '视图1/2的锚点，视图3为 NULL',
  `view_key`          VARCHAR(64)  NOT NULL                COMMENT '唯一键，如 1:10086 / 3:none',
  `layout_json`       MEDIUMTEXT   NOT NULL                COMMENT '布局结果 JSON：{canvas, nodes:[{id,x,y,gen}], edges:[{from,to,type}]}',
  `node_count`        INT          NOT NULL DEFAULT 0      COMMENT '节点数',
  `family_rev`        BIGINT       NOT NULL DEFAULT 0      COMMENT '生成时 family.data_rev，用于失效判断',
  `calc_ms`           INT          NOT NULL DEFAULT 0      COMMENT '计算耗时（毫秒），用于性能监控',
  `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_view_key` (`family_id`, `view_key`),
  KEY `idx_family_rev` (`family_id`, `family_rev`),
  KEY `idx_anchor` (`anchor_person_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='家族树布局缓存';


-- ============================================================================
-- 13. audit_log  审计日志
-- ============================================================================
DROP TABLE IF EXISTS `audit_log`;
CREATE TABLE `audit_log` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `family_id`         BIGINT UNSIGNED  NULL,
  `user_id`           BIGINT UNSIGNED  NULL                COMMENT '操作人，NULL 表示系统',
  `action`            VARCHAR(64)  NOT NULL                COMMENT '如 claim.approve / person.update / invite.create / person.merge / data.export',
  `target_table`      VARCHAR(32)      NULL,
  `target_id`         BIGINT UNSIGNED  NULL,
  `detail`            JSON             NULL                COMMENT '操作详情（脱敏后）',
  `ip`                VARCHAR(64)      NULL,
  `user_agent`        VARCHAR(255)     NULL,
  `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_family_time` (`family_id`, `created_at`),
  KEY `idx_user` (`user_id`, `created_at`),
  KEY `idx_action` (`action`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='审计日志';


-- ============================================================================
-- 14. notification  通知
-- ============================================================================
DROP TABLE IF EXISTS `notification`;
CREATE TABLE `notification` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id`           BIGINT UNSIGNED NOT NULL             COMMENT '接收人',
  `family_id`         BIGINT UNSIGNED  NULL,
  `type`              TINYINT      NOT NULL                COMMENT '1认领审核结果 2我的提交被审 3有人认领了我附近的节点 4填写冲突待确认 5邀请被接受 6待补全提醒',
  `title`             VARCHAR(128) NOT NULL,
  `body`              VARCHAR(512)     NULL,
  `payload`           JSON             NULL                COMMENT '跳转参数：{personId, revisionId, claimId}',
  `is_read`           TINYINT      NOT NULL DEFAULT 0,
  `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_user_read` (`user_id`, `is_read`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='通知';


SET FOREIGN_KEY_CHECKS = 1;


-- ============================================================================
-- 常用查询示例
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 示例 A：查某人的直系上下 N 代（用于"我的一家"和"待补全清单"）
--        :anchor 为锚点 person_id，:up / :down 为向上/向下代数
-- ---------------------------------------------------------------------------
-- WITH RECURSIVE
-- ancestors AS (
--   SELECT p.id, p.generation, p.name, p.lineage_role, 0 AS lvl, CAST(p.id AS CHAR(200)) AS path
--   FROM person p WHERE p.id = :anchor AND p.status = 1
--   UNION ALL
--   SELECT p.id, p.generation, p.name, p.lineage_role, a.lvl + 1, CONCAT(a.path, ',', p.id)
--   FROM ancestors a
--   JOIN parent_child pc ON pc.child_id = a.id AND pc.is_primary = 1 AND pc.status = 1
--   JOIN family_union u  ON u.id = pc.union_id AND u.status = 1
--   JOIN person p        ON p.id = u.partner_a_id AND p.status = 1
--   WHERE a.lvl < :up AND FIND_IN_SET(p.id, a.path) = 0     -- 防环
-- ),
-- descendants AS (
--   SELECT p.id, p.generation, p.name, p.lineage_role, 0 AS lvl, CAST(p.id AS CHAR(200)) AS path
--   FROM person p WHERE p.id = :anchor AND p.status = 1
--   UNION ALL
--   SELECT p.id, p.generation, p.name, p.lineage_role, d.lvl + 1, CONCAT(d.path, ',', p.id)
--   FROM descendants d
--   JOIN parent_child pc ON pc.union_id IN (
--          SELECT u.id FROM family_union u
--          WHERE (u.partner_a_id = d.id OR u.partner_b_id = d.id) AND u.status = 1)
--      AND pc.is_primary = 1 AND pc.status = 1
--   JOIN person p ON p.id = pc.child_id AND p.status = 1
--   WHERE d.lvl < :down AND FIND_IN_SET(p.id, d.path) = 0
-- )
-- SELECT 'ancestor' AS direction, * FROM ancestors WHERE lvl > 0
-- UNION ALL
-- SELECT 'descendant' AS direction, * FROM descendants WHERE lvl > 0;

-- ---------------------------------------------------------------------------
-- 示例 B：字辈校验（仅提示，不阻断提交）
--        只对 lineage_role=1（本族男丁）且填了 generation_name 的人计算
-- ---------------------------------------------------------------------------
-- SELECT p.id, p.name, p.generation, p.generation_name,
--        LOCATE(p.generation_name, f.generation_poem) AS pos_in_poem,
--        CASE WHEN LOCATE(p.generation_name, f.generation_poem) > 0
--             THEN f.first_gen_label + LOCATE(p.generation_name, f.generation_poem) - 1
--             ELSE NULL END AS expected_generation
-- FROM person p
-- JOIN family f ON f.id = p.family_id
-- WHERE p.family_id = :familyId
--   AND p.lineage_role = 1
--   AND p.generation_name IS NOT NULL AND p.generation_name <> ''
--   AND f.gen_poem_enabled = 1
-- HAVING expected_generation IS NOT NULL AND expected_generation <> p.generation;

-- ---------------------------------------------------------------------------
-- 示例 C：生成编辑码的权限快照（锚点向上3代、向下2代、含配偶）
--        实际由应用层执行，这里给出 SQL 侧的等价逻辑供对照
-- ---------------------------------------------------------------------------
-- 见接口定义文档 POST /api/invite/generate 中的伪码

-- ---------------------------------------------------------------------------
-- 示例 D：待补全清单（"配某氏"类任务，接力修谱的第一批弹药）
-- ---------------------------------------------------------------------------
-- SELECT u.id AS union_id, u.partner_a_id, u.partner_b_id,
--        pa.name AS carrier_name
-- FROM family_union u
-- JOIN person pa ON pa.id = u.partner_a_id
-- LEFT JOIN person pb ON pb.id = u.partner_b_id
-- WHERE u.family_id = :familyId
--   AND u.status = 1
--   AND (u.partner_b_id IS NULL OR pb.name IS NULL)
-- ORDER BY pa.generation;

-- ---------------------------------------------------------------------------
-- 示例 E：一致性巡检（替代物理外键）
-- ---------------------------------------------------------------------------
-- -- E1 孤儿子女
-- SELECT pc.id FROM parent_child pc
-- LEFT JOIN person p ON p.id = pc.child_id
-- WHERE p.id IS NULL OR p.status = 0;
--
-- -- E2 孤儿 union
-- SELECT pc.id FROM parent_child pc
-- LEFT JOIN family_union u ON u.id = pc.union_id
-- WHERE u.id IS NULL OR u.status = 0;
--
-- -- E3 主谱关系缺失（有 parent_child 但无 is_primary=1）
-- SELECT child_id FROM parent_child WHERE status = 1
-- GROUP BY child_id HAVING SUM(is_primary = 1) = 0;
--
-- -- E4 主谱关系重复（一个孩子有多个主谱父）
-- SELECT child_id FROM parent_child WHERE status = 1 AND is_primary = 1
-- GROUP BY child_id HAVING COUNT(*) > 1;
--
-- -- E5 跨族挂靠（子女与 union 不在同一族）
-- SELECT pc.id FROM parent_child pc
-- JOIN person c  ON c.id = pc.child_id
-- JOIN family_union u ON u.id = pc.union_id
-- WHERE c.family_id <> u.family_id;

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

-- ============================================================================
-- END OF SCHEMA
-- ============================================================================
