-- DROP SCHEMA prod;

CREATE SCHEMA prod AUTHORIZATION postgres;

-- DROP SEQUENCE prod.categories_id_seq;

CREATE SEQUENCE prod.categories_id_seq
    INCREMENT BY 1
    MINVALUE 1
    MAXVALUE 2147483647
    START 1
    CACHE 1
    NO CYCLE;

-- Permissions

ALTER SEQUENCE prod.categories_id_seq OWNER TO postgres;
GRANT ALL ON SEQUENCE prod.categories_id_seq TO postgres;
GRANT ALL ON SEQUENCE prod.categories_id_seq TO prod_todo_grocery;

-- DROP SEQUENCE prod.task_assignment_id_seq;

CREATE SEQUENCE prod.task_assignment_id_seq
    INCREMENT BY 1
    MINVALUE 1
    MAXVALUE 2147483647
    START 1
    CACHE 1
    NO CYCLE;

-- Permissions

ALTER SEQUENCE prod.task_assignment_id_seq OWNER TO postgres;
GRANT ALL ON SEQUENCE prod.task_assignment_id_seq TO postgres;
GRANT ALL ON SEQUENCE prod.task_assignment_id_seq TO prod_todo_grocery;

-- DROP SEQUENCE prod.tasks_id_seq;

CREATE SEQUENCE prod.tasks_id_seq
    INCREMENT BY 1
    MINVALUE 1
    MAXVALUE 2147483647
    START 1
    CACHE 1
    NO CYCLE;

-- Permissions

ALTER SEQUENCE prod.tasks_id_seq OWNER TO postgres;
GRANT ALL ON SEQUENCE prod.tasks_id_seq TO postgres;
GRANT ALL ON SEQUENCE prod.tasks_id_seq TO prod_todo_grocery;
-- prod.categories definition

-- Drop table

-- DROP TABLE prod.categories;

CREATE TABLE prod.categories (
    id serial4 NOT NULL, -- Unique category ID
    "name" varchar(255) NOT NULL, -- Category name (unique)
    "position" int4 DEFAULT 0 NULL,
    created_at timestamp DEFAULT now() NULL,
    updated_at timestamp DEFAULT now() NULL,
    CONSTRAINT categories_name_key UNIQUE (name),
    CONSTRAINT categories_pkey PRIMARY KEY (id)
);
COMMENT ON TABLE prod.categories IS 'Stores categories of tasks';

-- Column comments

COMMENT ON COLUMN prod.categories.id IS 'Unique category ID';
COMMENT ON COLUMN prod.categories."name" IS 'Category name (unique)';

-- Permissions

ALTER TABLE prod.categories OWNER TO postgres;
GRANT ALL ON TABLE prod.categories TO postgres;
GRANT DELETE, SELECT, INSERT, UPDATE ON TABLE prod.categories TO prod_todo_grocery;


-- prod.tasks definition

-- Drop table

-- DROP TABLE prod.tasks;

CREATE TABLE prod.tasks (
    id serial4 NOT NULL, -- Unique task ID
    "name" varchar(255) NOT NULL, -- Task name
    description text NULL, -- Task description
    is_done bool DEFAULT false NULL,
    created_at timestamp DEFAULT now() NULL,
    updated_at timestamp DEFAULT now() NULL,
    CONSTRAINT tasks_pkey PRIMARY KEY (id)
);
COMMENT ON TABLE prod.tasks IS 'Stores tasks';

-- Column comments

COMMENT ON COLUMN prod.tasks.id IS 'Unique task ID';
COMMENT ON COLUMN prod.tasks."name" IS 'Task name';
COMMENT ON COLUMN prod.tasks.description IS 'Task description';

-- Permissions

ALTER TABLE prod.tasks OWNER TO postgres;
GRANT ALL ON TABLE prod.tasks TO postgres;
GRANT DELETE, SELECT, INSERT, UPDATE ON TABLE prod.tasks TO prod_todo_grocery;


-- prod.task_assignment definition

-- Drop table

-- DROP TABLE prod.task_assignment;

CREATE TABLE prod.task_assignment (
    id serial4 NOT NULL, -- Unique task assignment ID
    task_id int4 NOT NULL, -- Reference to a task
    category_id int4 NOT NULL, -- Reference to a category
    assigned_at timestamp DEFAULT now() NULL, -- Timestamp of assignment
    CONSTRAINT task_assignment_pkey PRIMARY KEY (id),
    CONSTRAINT fk_category FOREIGN KEY (category_id) REFERENCES prod.categories(id) ON DELETE CASCADE,
    CONSTRAINT fk_task FOREIGN KEY (task_id) REFERENCES prod.tasks(id) ON DELETE CASCADE
);
COMMENT ON TABLE prod.task_assignment IS 'Links tasks to categories';

-- Column comments

COMMENT ON COLUMN prod.task_assignment.id IS 'Unique task assignment ID';
COMMENT ON COLUMN prod.task_assignment.task_id IS 'Reference to a task';
COMMENT ON COLUMN prod.task_assignment.category_id IS 'Reference to a category';
COMMENT ON COLUMN prod.task_assignment.assigned_at IS 'Timestamp of assignment';

-- Permissions

ALTER TABLE prod.task_assignment OWNER TO postgres;
GRANT ALL ON TABLE prod.task_assignment TO postgres;
GRANT DELETE, SELECT, INSERT, UPDATE ON TABLE prod.task_assignment TO prod_todo_grocery;




-- Permissions

GRANT ALL ON SCHEMA prod TO postgres;
GRANT USAGE ON SCHEMA prod TO prod_todo_grocery;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA prod GRANT DELETE, SELECT, INSERT, UPDATE ON TABLES TO prod_todo_grocery;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA prod GRANT SELECT, USAGE, UPDATE ON SEQUENCES TO prod_todo_grocery;
