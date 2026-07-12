-- personal_data 健康表冻结（V3 P3 收尾，docs/subpath-agent-plan.md §3.4）
-- 迁移核对通过后在 personal_data 库执行，防旧链路（Hermes 直写 SQL）复活双写。
-- life_memories 非健康数据，留在 personal_data 继续可写，不冻结。
--
-- 用法（占位符替换后在 NAS 上执行）：
--   psql -h 127.0.0.1 -p <PORT> -U <SUPERUSER> -d personal_data -f freeze_personal_data.sql
--
-- 方案 A（默认）：REVOKE 写权限。
-- ⚠ REVOKE 管不住表 owner——先查 owner 是谁：
--   SELECT tablename, tableowner FROM pg_tables WHERE schemaname='public';
-- 若 Hermes 旧链路用的正是 owner 角色，改用文件末尾的方案 B（改名 *_frozen）。

BEGIN;

DO $$
DECLARE
    t text;
    r text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'diet_records', 'weight_records', 'activity_records',
        'daily_summary', 'daily_drinks', 'body_measurements',
        'personal_info', 'exercise_summary', 'monthly_activity'
    ] LOOP
        EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.%I FROM PUBLIC', t);
        -- 逐个撤掉除执行者外所有有写权限的角色（含旧 Hermes 角色，无需事先知道名字）
        FOR r IN
            SELECT DISTINCT grantee FROM information_schema.role_table_grants
            WHERE table_schema = 'public' AND table_name = t
              AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')
              AND grantee NOT IN ('PUBLIC', current_user)
        LOOP
            EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.%I FROM %I', t, r);
        END LOOP;
    END LOOP;
END $$;

COMMIT;

-- 验证（应全为 0 行）：
--   SELECT grantee, table_name, privilege_type
--   FROM information_schema.role_table_grants
--   WHERE table_schema='public' AND privilege_type IN ('INSERT','UPDATE','DELETE')
--     AND table_name IN ('diet_records','weight_records','activity_records',
--                        'daily_summary','daily_drinks','body_measurements',
--                        'personal_info','exercise_summary','monthly_activity');

-- ------------------------------------------------------------------
-- 方案 B（owner 即写入角色时用；与方案 A 二选一，取消注释执行）：
-- 改名后旧脚本 SQL 直接报错 relation does not exist，效果最彻底。
-- ------------------------------------------------------------------
-- BEGIN;
-- ALTER TABLE public.diet_records       RENAME TO diet_records_frozen;
-- ALTER TABLE public.weight_records     RENAME TO weight_records_frozen;
-- ALTER TABLE public.activity_records   RENAME TO activity_records_frozen;
-- ALTER TABLE public.daily_summary      RENAME TO daily_summary_frozen;
-- ALTER TABLE public.daily_drinks       RENAME TO daily_drinks_frozen;
-- ALTER TABLE public.body_measurements  RENAME TO body_measurements_frozen;
-- ALTER TABLE public.personal_info      RENAME TO personal_info_frozen;
-- ALTER TABLE public.exercise_summary   RENAME TO exercise_summary_frozen;
-- ALTER TABLE public.monthly_activity   RENAME TO monthly_activity_frozen;
-- COMMIT;
