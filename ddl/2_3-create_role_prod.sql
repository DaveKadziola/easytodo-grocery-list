\connect todo_grocery;
-- DROP ROLE prod_todo_grocery;

CREATE ROLE prod_todo_grocery WITH
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    INHERIT
	LOGIN
    NOREPLICATION
    NOBYPASSRLS
    CONNECTION LIMIT -1;