-- Отдельная база в тестовом кластере с существующей ролью concord_reader.
\set ON_ERROR_STOP on
CREATE DATABASE concord_multi_01 WITH ENCODING 'UTF8' TEMPLATE template0;
\connect concord_multi_01
BEGIN;
CREATE SCHEMA registry;
CREATE TABLE registry.people (person_id text PRIMARY KEY, full_name text NOT NULL);
CREATE TABLE registry.contacts (
    contact_id integer PRIMARY KEY,
    person_id text NOT NULL REFERENCES registry.people(person_id),
    email text NOT NULL
);
-- Четыре разных человека в каждой группе. Первый и второй — однофамильцы.
CREATE TEMP VIEW fixture AS
SELECT n, lpad(n::text, 7, '0') AS id, (n - 1) % 4 + 1 AS slot,
       lpad(((n - 1) / 4)::text, 6, '0') AS grp
FROM generate_series(1, 1000000) AS seq(n);
INSERT INTO registry.people
SELECT id, CASE slot WHEN 3 THEN 'Петров ' WHEN 4 THEN 'Сидоров ' ELSE 'Иванов ' END || grp
FROM fixture;
INSERT INTO registry.contacts
SELECT n, id, CASE WHEN n <= 32 AND slot = 2 THEN 'pool@sample.test'
    ELSE (ARRAY['a','b','c','d'])[slot] || grp || '@sample.test' END
FROM fixture;
CREATE INDEX contacts_person_idx ON registry.contacts(person_id);
ANALYZE registry.people;
ANALYZE registry.contacts;
GRANT CONNECT ON DATABASE concord_multi_01 TO concord_reader;
GRANT USAGE ON SCHEMA registry TO concord_reader;
GRANT SELECT ON registry.people, registry.contacts TO concord_reader;
COMMIT;
