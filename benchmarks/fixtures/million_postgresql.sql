-- Запуск через psql в отдельном тестовом кластере с существующей ролью concord_reader.
\set ON_ERROR_STOP on
CREATE DATABASE concord_million_01;
\connect concord_million_01
BEGIN;
CREATE SCHEMA registry;
CREATE TABLE registry.people (person_id text PRIMARY KEY, full_name text NOT NULL);
CREATE TABLE registry.contacts (
    contact_id integer PRIMARY KEY,
    person_id text NOT NULL REFERENCES registry.people(person_id),
    email text NOT NULL
);
INSERT INTO registry.people
SELECT lpad(n::text, 7, '0'), 'Person ' || lpad(n::text, 7, '0')
FROM generate_series(1, 1000000) AS seq(n);
INSERT INTO registry.contacts
SELECT n, lpad(n::text, 7, '0'), 'p' || lpad(n::text, 7, '0') || '@sample.test'
FROM generate_series(1, 1000000) AS seq(n);
CREATE INDEX contacts_person_idx ON registry.contacts(person_id);
ANALYZE registry.people;
ANALYZE registry.contacts;
GRANT CONNECT ON DATABASE concord_million_01 TO concord_reader;
GRANT USAGE ON SCHEMA registry TO concord_reader;
GRANT SELECT ON registry.people, registry.contacts TO concord_reader;
COMMIT;
