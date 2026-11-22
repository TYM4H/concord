-- Отдельная база в тестовом экземпляре с существующим concord_reader@localhost.
SET NAMES utf8mb4;
CREATE DATABASE concord_multi_01 CHARACTER SET utf8mb4;
USE concord_multi_01;
CREATE TABLE customers (
    customer_id integer PRIMARY KEY,
    full_name varchar(40) NOT NULL,
    email varchar(64) NOT NULL
);
-- Первые две строки — дубли первого человека слева с разными исходными ID.
INSERT INTO customers (customer_id, full_name, email)
WITH digits(d) AS (
    SELECT 0 UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
    UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9
), seq(n) AS (
    SELECT 1 + a.d + 10*b.d + 100*c.d + 1000*e.d + 10000*f.d + 100000*g.d
    FROM digits a CROSS JOIN digits b CROSS JOIN digits c CROSS JOIN digits e
    CROSS JOIN digits f CROSS JOIN digits g
), fixture AS (
    SELECT n, mod(n-1,4)+1 AS slot, lpad((n-1) DIV 4,6,'0') AS grp FROM seq
)
SELECT n + 1000000,
       concat(CASE slot WHEN 3 THEN 'ПЕТРОВ ' WHEN 4 THEN 'СИДОРОВ ' ELSE 'ИВАНОВ ' END, grp),
       CASE WHEN n <= 32 AND slot = 4 THEN 'pool@sample.test'
            ELSE concat(IF(slot=4, 'd', 'a'), grp, '@sample.test') END
FROM fixture ORDER BY n;
ANALYZE TABLE customers;
GRANT SELECT ON concord_multi_01.* TO 'concord_reader'@'localhost';
