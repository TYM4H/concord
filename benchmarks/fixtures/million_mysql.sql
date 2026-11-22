-- Запуск клиентом mysql в отдельном тестовом экземпляре с concord_reader@localhost.
CREATE DATABASE concord_million_01 CHARACTER SET utf8mb4;
USE concord_million_01;
CREATE TABLE customers (
    customer_id integer PRIMARY KEY,
    full_name varchar(40) NOT NULL,
    email varchar(64) NOT NULL
);
INSERT INTO customers (customer_id, full_name, email)
WITH digits(d) AS (
    SELECT 0 UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
    UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9
), seq(n) AS (
    SELECT 1 + a.d + 10*b.d + 100*c.d + 1000*e.d + 10000*f.d + 100000*g.d
    FROM digits a CROSS JOIN digits b CROSS JOIN digits c CROSS JOIN digits e
    CROSS JOIN digits f CROSS JOIN digits g
)
SELECT n + 1000000, concat(IF(mod(n,4) IN (1,2), 'PERSON ', 'Other '), lpad(n,7,'0')),
       concat(IF(mod(n,4) IN (1,3), 'p', 'new'), lpad(n,7,'0'), '@sample.test')
FROM seq ORDER BY n;
ANALYZE TABLE customers;
GRANT SELECT ON concord_million_01.* TO 'concord_reader'@'localhost';
