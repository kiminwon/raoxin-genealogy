-- 2026-10-22 把认领「陈启华」的账号提为理事会成员（role=2）
--
-- 背景（启明拍板）：真正的房长年纪大、不操作智能机，实际编辑谱文的是
-- 理事会里会用手机的年轻人。角色不是认领来的，是指定的——族管理员调
-- POST /admin/role 授予。这一条是本轮"让人先能用"的一次性授予，
-- 之后都走接口。
--
-- 双写：user_family.role 是真相，app_user.role 是"当前族"快照。
-- 只改一边会出现"接口说你能编、端上说不能"。
-- 只影响 claimed_person_id 指向「陈启华」的那一行，不动其他人。

UPDATE user_family uf
JOIN person p ON p.id = uf.claimed_person_id
SET uf.role = 2
WHERE p.name = '陈启华'
  AND uf.status = 1;

UPDATE app_user u
JOIN user_family uf ON uf.user_id = u.id AND uf.family_id = u.family_id
JOIN person p ON p.id = uf.claimed_person_id
SET u.role = 2
WHERE p.name = '陈启华'
  AND uf.status = 1
  AND u.claimed_person_id = uf.claimed_person_id;
