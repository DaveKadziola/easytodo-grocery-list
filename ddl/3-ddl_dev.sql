-- DROP SCHEMA dev;

CREATE SCHEMA dev AUTHORIZATION postgres;

-- DROP SEQUENCE dev.categories_id_seq;

CREATE SEQUENCE dev.categories_id_seq
    INCREMENT BY 1
    MINVALUE 1
    MAXVALUE 2147483647
    START 1
    CACHE 1
    NO CYCLE;

-- Permissions

ALTER SEQUENCE dev.categories_id_seq OWNER TO postgres;
GRANT ALL ON SEQUENCE dev.categories_id_seq TO postgres;
GRANT ALL ON SEQUENCE dev.categories_id_seq TO dev_todo_grocery;

-- DROP SEQUENCE dev.task_assignment_id_seq;

CREATE SEQUENCE dev.task_assignment_id_seq
    INCREMENT BY 1
    MINVALUE 1
    MAXVALUE 2147483647
    START 1
    CACHE 1
    NO CYCLE;

-- Permissions

ALTER SEQUENCE dev.task_assignment_id_seq OWNER TO postgres;
GRANT ALL ON SEQUENCE dev.task_assignment_id_seq TO postgres;
GRANT ALL ON SEQUENCE dev.task_assignment_id_seq TO dev_todo_grocery;

-- DROP SEQUENCE dev.tasks_id_seq;

CREATE SEQUENCE dev.tasks_id_seq
    INCREMENT BY 1
    MINVALUE 1
    MAXVALUE 2147483647
    START 1
    CACHE 1
    NO CYCLE;

-- Permissions

ALTER SEQUENCE dev.tasks_id_seq OWNER TO postgres;
GRANT ALL ON SEQUENCE dev.tasks_id_seq TO postgres;
GRANT ALL ON SEQUENCE dev.tasks_id_seq TO dev_todo_grocery;
-- dev.categories definition

-- Drop table

-- DROP TABLE dev.categories;

CREATE TABLE dev.categories (
    id serial4 NOT NULL, -- Unique category ID
    "name" varchar(255) NOT NULL, -- Category name (unique)
    "position" int4 DEFAULT 0 NULL,
    created_at timestamp DEFAULT now() NULL,
    updated_at timestamp DEFAULT now() NULL,
    CONSTRAINT categories_name_key UNIQUE (name),
    CONSTRAINT categories_pkey PRIMARY KEY (id)
);
COMMENT ON TABLE dev.categories IS 'Stores categories of tasks';

-- Column comments

COMMENT ON COLUMN dev.categories.id IS 'Unique category ID';
COMMENT ON COLUMN dev.categories."name" IS 'Category name (unique)';

-- Permissions

ALTER TABLE dev.categories OWNER TO postgres;
GRANT ALL ON TABLE dev.categories TO postgres;
GRANT DELETE, SELECT, INSERT, UPDATE ON TABLE dev.categories TO dev_todo_grocery;


-- dev.tasks definition

-- Drop table

-- DROP TABLE dev.tasks;

CREATE TABLE dev.tasks (
    id serial4 NOT NULL, -- Unique task ID
    "name" varchar(255) NOT NULL, -- Task name
    description text NULL, -- Task description
    is_done bool DEFAULT false NULL,
    created_at timestamp DEFAULT now() NULL,
    updated_at timestamp DEFAULT now() NULL,
    CONSTRAINT tasks_pkey PRIMARY KEY (id)
);
COMMENT ON TABLE dev.tasks IS 'Stores tasks';

-- Column comments

COMMENT ON COLUMN dev.tasks.id IS 'Unique task ID';
COMMENT ON COLUMN dev.tasks."name" IS 'Task name';
COMMENT ON COLUMN dev.tasks.description IS 'Task description';

-- Permissions

ALTER TABLE dev.tasks OWNER TO postgres;
GRANT ALL ON TABLE dev.tasks TO postgres;
GRANT DELETE, SELECT, INSERT, UPDATE ON TABLE dev.tasks TO dev_todo_grocery;


-- dev.task_assignment definition

-- Drop table

-- DROP TABLE dev.task_assignment;

CREATE TABLE dev.task_assignment (
    id serial4 NOT NULL, -- Unique task assignment ID
    task_id int4 NOT NULL, -- Reference to a task
    category_id int4 NOT NULL, -- Reference to a category
    assigned_at timestamp DEFAULT now() NULL, -- Timestamp of assignment
    CONSTRAINT task_assignment_pkey PRIMARY KEY (id),
    CONSTRAINT fk_category FOREIGN KEY (category_id) REFERENCES dev.categories(id) ON DELETE CASCADE,
    CONSTRAINT fk_task FOREIGN KEY (task_id) REFERENCES dev.tasks(id) ON DELETE CASCADE
);
COMMENT ON TABLE dev.task_assignment IS 'Links tasks to categories';

-- Column comments

COMMENT ON COLUMN dev.task_assignment.id IS 'Unique task assignment ID';
COMMENT ON COLUMN dev.task_assignment.task_id IS 'Reference to a task';
COMMENT ON COLUMN dev.task_assignment.category_id IS 'Reference to a category';
COMMENT ON COLUMN dev.task_assignment.assigned_at IS 'Timestamp of assignment';

-- Permissions

ALTER TABLE dev.task_assignment OWNER TO postgres;
GRANT ALL ON TABLE dev.task_assignment TO postgres;
GRANT DELETE, SELECT, INSERT, UPDATE ON TABLE dev.task_assignment TO dev_todo_grocery;




-- Permissions

GRANT ALL ON SCHEMA dev TO postgres;
GRANT USAGE ON SCHEMA dev TO dev_todo_grocery;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA dev GRANT DELETE, SELECT, INSERT, UPDATE ON TABLES TO dev_todo_grocery;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA dev GRANT SELECT, USAGE, UPDATE ON SEQUENCES TO dev_todo_grocery;
