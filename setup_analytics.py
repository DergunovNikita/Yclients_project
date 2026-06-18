"""
Создание аналитических представлений (views) в PostgreSQL.
Эти views используются BI-инструментами (Metabase и др.) для построения дашбордов.
"""
from sqlalchemy import text
from config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
from database import init_database

VIEWS = [
    # ----------------------------------------------------------------
    # 1. Выручка по дням (фактически оплаченные услуги + товары)
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_revenue_daily AS
    WITH service_revenue AS (
        SELECT
            a.date::date                                AS line_date,
            a.client_id                                 AS client_id,
            a.id                                        AS appointment_id,
            COALESCE(ft.amount, 0)                      AS revenue,
            'service'::text                             AS source
        FROM financial_transactions ft
        JOIN appointments a ON a.id = ft.record_id
        WHERE a.attendance = 1
          AND ft.sold_item_type = 'service'
        UNION ALL
        SELECT
            ft.date::date                               AS line_date,
            ft.client_id                                AS client_id,
            NULL::int                                   AS appointment_id,
            COALESCE(ft.amount, 0)                      AS revenue,
            'goods'::text                               AS source
        FROM financial_transactions ft
        WHERE ft.sold_item_type = 'goods_transaction'
    ),
    appointments_by_day AS (
        SELECT
            a.date::date                                AS line_date,
            COUNT(DISTINCT a.id)                        AS appointments_count,
            COUNT(DISTINCT a.client_id)                 AS unique_clients
        FROM appointments a
        WHERE a.attendance = 1
        GROUP BY a.date::date
    ),
    service_before_discount AS (
        SELECT
            a.date::date                                AS line_date,
            COALESCE(SUM(t.first_cost * t.amount), 0)   AS revenue_before_discount
        FROM appointments a
        LEFT JOIN transactions t ON t.appointment_id = a.id
        WHERE a.attendance = 1
        GROUP BY a.date::date
    )
    SELECT
        sr.line_date                                    AS visit_date,
        COALESCE(MAX(abd.appointments_count), 0)        AS appointments_count,
        COALESCE(SUM(sr.revenue), 0)                    AS revenue,
        COALESCE(MAX(sbd.revenue_before_discount), 0)   AS revenue_before_discount,
        COALESCE(MAX(abd.unique_clients), 0)            AS unique_clients,
        COALESCE(SUM(sr.revenue) FILTER (WHERE sr.source = 'service'), 0) AS service_revenue,
        COALESCE(SUM(sr.revenue) FILTER (WHERE sr.source = 'goods'),   0) AS goods_revenue
    FROM service_revenue sr
    LEFT JOIN appointments_by_day abd ON abd.line_date = sr.line_date
    LEFT JOIN service_before_discount sbd ON sbd.line_date = sr.line_date
    WHERE sr.line_date IS NOT NULL
    GROUP BY sr.line_date
    ORDER BY sr.line_date ASC
    """,

    # ----------------------------------------------------------------
    # 2. Выручка по сотрудникам (фактически оплаченные услуги + товары)
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_revenue_by_staff AS
    WITH revenue_lines AS (
        SELECT
            a.staff_id                                  AS staff_id,
            a.client_id                                 AS client_id,
            a.id                                        AS appointment_id,
            COALESCE(ft.amount, 0)                      AS revenue,
            'service'::text                             AS source
        FROM financial_transactions ft
        JOIN appointments a ON a.id = ft.record_id
        WHERE a.attendance = 1
          AND ft.sold_item_type = 'service'
        UNION ALL
        SELECT
            ft.master_id                                AS staff_id,
            ft.client_id                                AS client_id,
            NULL::int                                   AS appointment_id,
            COALESCE(ft.amount, 0)                      AS revenue,
            'goods'::text                               AS source
        FROM financial_transactions ft
        WHERE ft.sold_item_type = 'goods_transaction'
    )
    SELECT
        s.id                                            AS staff_id,
        s.name                                          AS staff_name,
        s.position                                      AS staff_position,
        COUNT(DISTINCT rl.appointment_id)               AS appointments_count,
        COUNT(DISTINCT rl.client_id)                    AS unique_clients,
        COALESCE(SUM(rl.revenue), 0)                    AS revenue,
        COALESCE(SUM(rl.revenue) FILTER (WHERE rl.source = 'service'), 0) AS service_revenue,
        COALESCE(SUM(rl.revenue) FILTER (WHERE rl.source = 'goods'),   0) AS goods_revenue
    FROM staff s
    LEFT JOIN revenue_lines rl ON rl.staff_id = s.id
    GROUP BY s.id, s.name, s.position
    ORDER BY revenue DESC
    """,

    # ----------------------------------------------------------------
    # 3. Популярные услуги (по количеству и выручке)
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_popular_services AS
    WITH sold AS (
        SELECT
            a.company_id,
            t.service_id,
            COALESCE(NULLIF(t.service_title, ''), svc.title)::varchar AS service_title,
            SUM(t.amount)                               AS total_sold
        FROM transactions t
        JOIN appointments a ON a.id = t.appointment_id
        LEFT JOIN service_catalog svc
            ON  svc.company_id = a.company_id
            AND svc.service_id = t.service_id
        WHERE a.attendance = 1
        GROUP BY a.company_id, t.service_id, COALESCE(NULLIF(t.service_title, ''), svc.title)::varchar
    ),
    paid AS (
        SELECT
            a.company_id,
            ft.sold_item_id                             AS service_id,
            COALESCE(SUM(ft.amount), 0)                 AS total_revenue
        FROM financial_transactions ft
        JOIN appointments a ON a.id = ft.record_id
        WHERE a.attendance = 1
          AND ft.sold_item_type = 'service'
        GROUP BY a.company_id, ft.sold_item_id
    )
    SELECT
        sold.service_id,
        sold.service_title,
        sold.total_sold,
        COALESCE(paid.total_revenue, 0)                 AS total_revenue,
        ROUND((COALESCE(paid.total_revenue, 0) / NULLIF(sold.total_sold, 0))::numeric, 2) AS avg_price,
        sold.company_id,
        c.title                                         AS company_name
    FROM sold
    LEFT JOIN paid
        ON  paid.company_id = sold.company_id
        AND paid.service_id = sold.service_id
    LEFT JOIN companies c ON c.id = sold.company_id
    ORDER BY total_sold DESC
    """,

    # ----------------------------------------------------------------
    # 4. Загрузка сотрудников (часы работы)
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_staff_workload AS
    SELECT
        s.id                                            AS staff_id,
        s.name                                          AS staff_name,
        a.date                                          AS work_date,
        COUNT(a.id)                                     AS appointments_count,
        COALESCE(SUM(a.seance_length), 0) / 3600.0      AS hours_booked
    FROM staff s
    JOIN appointments a ON a.staff_id = s.id
    GROUP BY s.id, s.name, a.date
    ORDER BY a.date ASC, hours_booked DESC
    """,

    # ----------------------------------------------------------------
    # 5. Клиентская аналитика (траты: услуги + товары)
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_client_analytics AS
    WITH service_spent AS (
        SELECT
            a.client_id                                 AS client_id,
            COUNT(DISTINCT a.id)                        AS appointments_count,
            COALESCE(SUM(ft.amount), 0)                 AS revenue
        FROM appointments a
        LEFT JOIN financial_transactions ft
            ON  ft.record_id = a.id
            AND ft.sold_item_type = 'service'
        WHERE a.attendance = 1
        GROUP BY a.client_id
    ),
    goods_spent AS (
        SELECT
            ft.client_id                                AS client_id,
            COALESCE(SUM(ft.amount), 0)                 AS revenue
        FROM financial_transactions ft
        WHERE ft.sold_item_type = 'goods_transaction'
        GROUP BY ft.client_id
    )
    SELECT
        c.id                                            AS client_id,
        c.name                                          AS client_name,
        c.phone,
        c.visits_count,
        c.last_visit_date,
        c.discount,
        COALESCE(ss.appointments_count, 0)              AS total_appointments,
        COALESCE(ss.revenue, 0) + COALESCE(gs.revenue, 0) AS total_spent,
        CASE
            WHEN COALESCE(ss.appointments_count, 0) >= 10 THEN 'VIP'
            WHEN COALESCE(ss.appointments_count, 0) >= 5  THEN 'Постоянный'
            WHEN COALESCE(ss.appointments_count, 0) >= 2  THEN 'Повторный'
            ELSE 'Новый'
        END                                             AS client_segment,
        COALESCE(ss.revenue, 0)                         AS service_spent,
        COALESCE(gs.revenue, 0)                         AS goods_spent
    FROM clients c
    LEFT JOIN service_spent ss ON ss.client_id = c.id
    LEFT JOIN goods_spent   gs ON gs.client_id = c.id
    ORDER BY total_spent DESC
    """,

    # ----------------------------------------------------------------
    # 6. Выручка по месяцам (фактически оплаченные услуги + товары)
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_revenue_monthly AS
    WITH revenue_lines AS (
        SELECT
            TO_CHAR(a.date, 'YYYY-MM')                  AS month,
            a.client_id                                 AS client_id,
            a.id                                        AS appointment_id,
            COALESCE(ft.amount, 0)                      AS revenue,
            'service'::text                             AS source
        FROM financial_transactions ft
        JOIN appointments a ON a.id = ft.record_id
        WHERE a.attendance = 1
          AND ft.sold_item_type = 'service'
        UNION ALL
        SELECT
            TO_CHAR(ft.date, 'YYYY-MM')                 AS month,
            ft.client_id                                AS client_id,
            NULL::int                                   AS appointment_id,
            COALESCE(ft.amount, 0)                      AS revenue,
            'goods'::text                               AS source
        FROM financial_transactions ft
        WHERE ft.sold_item_type = 'goods_transaction'
    )
    SELECT
        month,
        COUNT(DISTINCT appointment_id)                  AS appointments_count,
        COUNT(DISTINCT client_id)                       AS unique_clients,
        COALESCE(SUM(revenue), 0)                       AS revenue,
        COALESCE(SUM(revenue) FILTER (WHERE source = 'service'), 0) AS service_revenue,
        COALESCE(SUM(revenue) FILTER (WHERE source = 'goods'),   0) AS goods_revenue
    FROM revenue_lines
    WHERE month IS NOT NULL
    GROUP BY month
    ORDER BY month ASC
    """,

    # ----------------------------------------------------------------
    # 7. Конверсия записей (пришёл / не пришёл)
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_attendance_stats AS
    SELECT
        a.date                                          AS visit_date,
        COUNT(*)                                        AS total_records,
        COUNT(*) FILTER (WHERE a.attendance = 1)        AS attended,
        COUNT(*) FILTER (WHERE a.attendance = -1)       AS no_show,
        COUNT(*) FILTER (WHERE a.attendance IN (0, 2))  AS pending,
        ROUND(
            100.0 * COUNT(*) FILTER (WHERE a.attendance = 1) / NULLIF(COUNT(*), 0),
            1
        )                                               AS attendance_rate_pct,
        COUNT(*)                                        AS available_records,
        COUNT(*) FILTER (WHERE a.attendance = 2)        AS confirmed,
        'local_available_records'::text                 AS data_scope
    FROM appointments a
    GROUP BY a.date
    ORDER BY a.date ASC
    """,

    # ----------------------------------------------------------------
    # 8. Финансовый поток по дням (из financial_transactions)
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_finance_daily AS
    SELECT
        ft.date::date                                   AS tx_date,
        COUNT(*)                                        AS transactions_count,
        COALESCE(SUM(CASE WHEN ft.amount > 0 THEN ft.amount ELSE 0 END), 0) AS income,
        COALESCE(SUM(CASE WHEN ft.amount < 0 THEN ABS(ft.amount) ELSE 0 END), 0) AS expense,
        COALESCE(SUM(ft.amount), 0)                     AS net_flow
    FROM financial_transactions ft
    GROUP BY ft.date::date
    ORDER BY tx_date ASC
    """,

    # ----------------------------------------------------------------
    # 9. Финансы по кассам
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_finance_by_account AS
    SELECT
        acc.account_id,
        acc.title                                       AS account_name,
        acc.type                                        AS account_type,
        COUNT(ft.id)                                    AS transactions_count,
        COALESCE(SUM(CASE WHEN ft.amount > 0 THEN ft.amount ELSE 0 END), 0) AS total_income,
        COALESCE(SUM(CASE WHEN ft.amount < 0 THEN ABS(ft.amount) ELSE 0 END), 0) AS total_expense,
        COALESCE(SUM(ft.amount), 0)                     AS balance,
        acc.company_id,
        c.title                                         AS company_name
    FROM account_catalog acc
    LEFT JOIN companies c ON c.id = acc.company_id
    LEFT JOIN financial_transactions ft
        ON  ft.account_id = acc.account_id
        AND ft.company_id = acc.company_id
    GROUP BY acc.company_id, c.title, acc.account_id, acc.title, acc.type
    ORDER BY total_income DESC
    """,

    # ----------------------------------------------------------------
    # 10. Финансы по месяцам
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_finance_monthly AS
    SELECT
        TO_CHAR(ft.date, 'YYYY-MM')                     AS month,
        COUNT(*)                                        AS transactions_count,
        COALESCE(SUM(CASE WHEN ft.amount > 0 THEN ft.amount ELSE 0 END), 0) AS income,
        COALESCE(SUM(CASE WHEN ft.amount < 0 THEN ABS(ft.amount) ELSE 0 END), 0) AS expense,
        COALESCE(SUM(ft.amount), 0)                     AS net_flow
    FROM financial_transactions ft
    GROUP BY TO_CHAR(ft.date, 'YYYY-MM')
    ORDER BY month ASC
    """,

    # ----------------------------------------------------------------
    # 11. Продажи товаров (из goods_transactions)
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_goods_sales AS
    SELECT
        gt.good_id,
        COALESCE(NULLIF(gt.good_title, ''), g.title, gt.good_id::text)::varchar AS good_title,
        g.cost                                          AS list_price,
        COUNT(gt.id)                                    AS total_transactions,
        COALESCE(SUM(gt.amount), 0)                     AS total_qty_sold,
        COALESCE(SUM(gt.cost), 0)                       AS total_revenue,
        ROUND(AVG(gt.cost_per_unit)::numeric, 2)        AS avg_sell_price,
        gt.company_id,
        c.title                                         AS company_name
    FROM goods_transactions gt
    LEFT JOIN good_catalog g
        ON  g.company_id = gt.company_id
        AND g.good_id = gt.good_id
    LEFT JOIN companies c ON c.id = gt.company_id
    GROUP BY gt.company_id, c.title, gt.good_id, COALESCE(NULLIF(gt.good_title, ''), g.title, gt.good_id::text), g.cost
    ORDER BY total_revenue DESC
    """,

    # ----------------------------------------------------------------
    # 12. Движение товаров по складам
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_goods_movement AS
    SELECT
        st.storage_id,
        st.title                                        AS storage_name,
        COUNT(gt.id)                                    AS transactions_count,
        COALESCE(SUM(gt.amount), 0)                     AS total_units,
        COALESCE(SUM(gt.cost), 0)                       AS total_cost,
        st.company_id,
        c.title                                         AS company_name
    FROM storage_catalog st
    LEFT JOIN companies c ON c.id = st.company_id
    LEFT JOIN goods_transactions gt
        ON  gt.storage_id = st.storage_id
        AND gt.company_id = st.company_id
    GROUP BY st.company_id, c.title, st.storage_id, st.title
    ORDER BY total_cost DESC
    """,

    # ----------------------------------------------------------------
    # 13. Рейтинг сотрудников по отзывам
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_staff_reviews AS
    SELECT
        s.id                                            AS staff_id,
        s.name                                          AS staff_name,
        COUNT(c.id)                                     AS reviews_count,
        ROUND(AVG(c.rating)::numeric, 2)                AS avg_rating,
        MIN(c.rating)                                   AS min_rating,
        MAX(c.rating)                                   AS max_rating
    FROM staff s
    LEFT JOIN comments c ON c.master_id = s.id AND c.rating IS NOT NULL
    GROUP BY s.id, s.name
    ORDER BY avg_rating DESC NULLS LAST
    """,

    # ----------------------------------------------------------------
    # 14. Отзывы по месяцам
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_reviews_monthly AS
    SELECT
        TO_CHAR(c.date, 'YYYY-MM')                      AS month,
        COUNT(*)                                        AS reviews_count,
        ROUND(AVG(c.rating)::numeric, 2)                AS avg_rating,
        COUNT(*) FILTER (WHERE c.rating >= 4)           AS positive,
        COUNT(*) FILTER (WHERE c.rating <= 2)           AS negative
    FROM comments c
    WHERE c.rating IS NOT NULL
    GROUP BY TO_CHAR(c.date, 'YYYY-MM')
    ORDER BY month ASC
    """,

    # ----------------------------------------------------------------
    # 15. Загрузка расписания (заполненность слотов)
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_schedule_utilization AS
    SELECT
        ss.staff_id,
        s.name                                          AS staff_name,
        ss.date,
        COUNT(ss.id)                                    AS schedule_slots,
        COUNT(DISTINCT a.id)                            AS booked_appointments
    FROM staff_schedules ss
    JOIN staff s ON s.id = ss.staff_id
    LEFT JOIN appointments a ON a.staff_id = ss.staff_id AND a.date = ss.date
    GROUP BY ss.staff_id, s.name, ss.date
    ORDER BY ss.date ASC
    """,

    # ----------------------------------------------------------------
    # 16. Справочник филиалов для фильтров
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_companies_lookup AS
    SELECT
        c.id                                            AS company_id,
        c.title                                         AS company_name,
        g.id                                            AS group_id,
        g.title                                         AS group_name,
        c.title || ' (' || c.id || ')'                  AS company_label
    FROM companies c
    LEFT JOIN groups g ON g.id = c.group_id
    ORDER BY c.title ASC, c.id ASC
    """,

    # ----------------------------------------------------------------
    # 17. Справочник услуг для фильтров
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_services_lookup AS
    SELECT
        s.service_id,
        s.title                                         AS service_name,
        s.category_title                                AS service_category,
        s.company_id,
        c.title                                         AS company_name,
        s.title || ' — ' || c.title                     AS service_label
    FROM service_catalog s
    LEFT JOIN companies c ON c.id = s.company_id
    ORDER BY s.title ASC, c.title ASC
    """,

    # ----------------------------------------------------------------
    # 18. Календарь для BI-фильтров
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_calendar AS
    WITH bounds AS (
        SELECT
            MIN(src.dt)                                 AS min_date,
            MAX(src.dt)                                 AS max_date
        FROM (
            SELECT a.date::date AS dt
            FROM appointments a
            WHERE a.date IS NOT NULL
            UNION ALL
            SELECT ft.date::date AS dt
            FROM financial_transactions ft
            WHERE ft.date IS NOT NULL
        ) src
    )
    SELECT
        gs.dt::date                                     AS calendar_date,
        EXTRACT(YEAR FROM gs.dt)::int                   AS year_num,
        EXTRACT(QUARTER FROM gs.dt)::int                AS quarter_num,
        CONCAT(EXTRACT(QUARTER FROM gs.dt)::int, ' кв. ', EXTRACT(YEAR FROM gs.dt)::int)
                                                        AS quarter_label_ru,
        DATE_TRUNC('quarter', gs.dt)::date              AS quarter_start_date,
        EXTRACT(MONTH FROM gs.dt)::int                  AS month_num,
        CASE EXTRACT(MONTH FROM gs.dt)::int
            WHEN 1 THEN 'Январь'
            WHEN 2 THEN 'Февраль'
            WHEN 3 THEN 'Март'
            WHEN 4 THEN 'Апрель'
            WHEN 5 THEN 'Май'
            WHEN 6 THEN 'Июнь'
            WHEN 7 THEN 'Июль'
            WHEN 8 THEN 'Август'
            WHEN 9 THEN 'Сентябрь'
            WHEN 10 THEN 'Октябрь'
            WHEN 11 THEN 'Ноябрь'
            ELSE 'Декабрь'
        END                                             AS month_name_ru,
        CONCAT(
            CASE EXTRACT(MONTH FROM gs.dt)::int
                WHEN 1 THEN 'Январь'
                WHEN 2 THEN 'Февраль'
                WHEN 3 THEN 'Март'
                WHEN 4 THEN 'Апрель'
                WHEN 5 THEN 'Май'
                WHEN 6 THEN 'Июнь'
                WHEN 7 THEN 'Июль'
                WHEN 8 THEN 'Август'
                WHEN 9 THEN 'Сентябрь'
                WHEN 10 THEN 'Октябрь'
                WHEN 11 THEN 'Ноябрь'
                ELSE 'Декабрь'
            END,
            ' ',
            EXTRACT(YEAR FROM gs.dt)::int
        )                                               AS month_label_ru,
        TO_CHAR(gs.dt, 'YYYY-MM')                       AS month_key,
        DATE_TRUNC('month', gs.dt)::date                AS month_start_date,
        EXTRACT(WEEK FROM gs.dt)::int                   AS week_num,
        EXTRACT(ISOYEAR FROM gs.dt)::int                AS iso_year_num,
        CONCAT('Неделя ', TO_CHAR(gs.dt, 'IW'), ' ', EXTRACT(ISOYEAR FROM gs.dt)::int)
                                                        AS iso_week_label_ru,
        DATE_TRUNC('week', gs.dt)::date                 AS week_start_date,
        EXTRACT(ISODOW FROM gs.dt)::int                 AS iso_weekday_num,
        CASE EXTRACT(ISODOW FROM gs.dt)::int
            WHEN 1 THEN 'Понедельник'
            WHEN 2 THEN 'Вторник'
            WHEN 3 THEN 'Среда'
            WHEN 4 THEN 'Четверг'
            WHEN 5 THEN 'Пятница'
            WHEN 6 THEN 'Суббота'
            ELSE 'Воскресенье'
        END                                             AS weekday_name_ru,
        TO_CHAR(gs.dt, 'YYYY-MM-DD')                    AS date_key
    FROM bounds b
    CROSS JOIN LATERAL generate_series(b.min_date, b.max_date, INTERVAL '1 day') AS gs(dt)
    ORDER BY gs.dt ASC
    """,

    # ----------------------------------------------------------------
    # 19. Обогащённая витрина записей и услуг
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_appointments_enriched AS
    WITH service_paid AS (
        SELECT
            ft.record_id                                AS appointment_id,
            ft.sold_item_id                             AS service_id,
            COALESCE(SUM(ft.amount), 0)                 AS service_revenue
        FROM financial_transactions ft
        WHERE ft.sold_item_type = 'service'
        GROUP BY ft.record_id, ft.sold_item_id
    )
    SELECT
        a.id                                            AS appointment_id,
        a.date::date                                    AS visit_date,
        cal.year_num,
        cal.quarter_num,
        cal.quarter_label_ru,
        cal.month_num,
        cal.month_name_ru,
        cal.month_label_ru,
        cal.month_key,
        cal.week_num,
        cal.iso_year_num,
        cal.iso_week_label_ru,
        cal.week_start_date,
        a.company_id,
        c.title                                         AS company_name,
        c.title || ' (' || a.company_id || ')'          AS company_label,
        a.staff_id,
        s.name                                          AS staff_name,
        a.client_id,
        cl.name                                         AS client_name,
        a.attendance,
        CASE
            WHEN a.attendance = 1 THEN 'Пришел'
            WHEN a.attendance = 2 THEN 'Подтвердил'
            WHEN a.attendance = 0 THEN 'Ожидается'
            WHEN a.attendance = -1 THEN 'Не пришел'
            ELSE 'Другое'
        END                                             AS attendance_label,
        t.service_id,
        COALESCE(NULLIF(t.service_title, ''), svc.title) AS service_title,
        svc.category_title                              AS service_category,
        t.amount                                        AS service_qty,
        t.cost                                          AS service_cost,
        t.first_cost                                    AS service_first_cost,
        COALESCE(sp.service_revenue, 0)                 AS service_revenue
    FROM appointments a
    LEFT JOIN transactions t ON t.appointment_id = a.id
    LEFT JOIN service_paid sp
        ON  sp.appointment_id = a.id
        AND sp.service_id = t.service_id
    LEFT JOIN service_catalog svc
        ON  svc.company_id = a.company_id
        AND svc.service_id = t.service_id
    LEFT JOIN companies c ON c.id = a.company_id
    LEFT JOIN staff s ON s.id = a.staff_id
    LEFT JOIN clients cl ON cl.id = a.client_id
    LEFT JOIN v_calendar cal ON cal.calendar_date = a.date::date
    ORDER BY a.date::date ASC, a.id ASC
    """,

    # ----------------------------------------------------------------
    # 20. Обогащённая витрина финансовых транзакций
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_financial_transactions_enriched AS
    SELECT
        ft.id                                           AS transaction_id,
        ft.date::date                                   AS tx_date,
        cal.year_num,
        cal.quarter_num,
        cal.quarter_label_ru,
        cal.month_num,
        cal.month_name_ru,
        cal.month_label_ru,
        cal.month_key,
        cal.week_num,
        cal.iso_year_num,
        cal.iso_week_label_ru,
        cal.week_start_date,
        ft.company_id,
        c.title                                         AS company_name,
        c.title || ' (' || ft.company_id || ')'         AS company_label,
        ft.account_id,
        acc.title                                       AS account_name,
        ft.client_id,
        cl.name                                         AS client_name,
        ft.master_id,
        s.name                                          AS staff_name,
        ft.record_id,
        ft.sold_item_id,
        ft.sold_item_type,
        CASE
            WHEN ft.sold_item_type = 'service' THEN 'Услуга'
            WHEN ft.sold_item_type = 'goods_transaction' THEN 'Товар'
            ELSE COALESCE(ft.sold_item_type, 'Не указано')
        END                                             AS sold_item_type_label,
        CASE
            WHEN ft.amount > 0 THEN 'Приход'
            WHEN ft.amount < 0 THEN 'Расход'
            ELSE 'Ноль'
        END                                             AS flow_direction,
        ft.amount,
        ft.comment
    FROM financial_transactions ft
    LEFT JOIN companies c ON c.id = ft.company_id
    LEFT JOIN account_catalog acc
        ON  acc.company_id = ft.company_id
        AND acc.account_id = ft.account_id
    LEFT JOIN clients cl ON cl.id = ft.client_id
    LEFT JOIN staff s ON s.id = ft.master_id
    LEFT JOIN v_calendar cal ON cal.calendar_date = ft.date::date
    ORDER BY ft.date::date ASC, ft.id ASC
    """,

    # ----------------------------------------------------------------
    # 21. Обогащённая витрина товарных транзакций
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_goods_transactions_enriched AS
    SELECT
        gt.id                                           AS goods_transaction_id,
        gt.company_id,
        c.title                                         AS company_name,
        c.title || ' (' || gt.company_id || ')'         AS company_label,
        gt.good_id,
        COALESCE(NULLIF(gt.good_title, ''), g.title, gt.good_id::text)::varchar AS good_title,
        gt.storage_id,
        COALESCE(NULLIF(gt.storage_title, ''), st.title)::varchar AS storage_name,
        gt.client_id,
        cl.name                                         AS client_name,
        gt.master_id,
        s.name                                          AS staff_name,
        gt.type_id,
        CASE
            WHEN gt.type_id = 1 THEN 'Продажа'
            WHEN gt.type_id = 3 THEN 'Поступление'
            WHEN gt.type_id = 4 THEN 'Списание'
            ELSE 'Другое'
        END                                             AS operation_type_label,
        gt.amount,
        gt.cost_per_unit,
        gt.cost,
        gt.discount,
        gt.date                                         AS transaction_date
    FROM goods_transactions gt
    LEFT JOIN companies c ON c.id = gt.company_id
    LEFT JOIN good_catalog g
        ON  g.company_id = gt.company_id
        AND g.good_id = gt.good_id
    LEFT JOIN storage_catalog st
        ON  st.company_id = gt.company_id
        AND st.storage_id = gt.storage_id
    LEFT JOIN clients cl ON cl.id = gt.client_id
    LEFT JOIN staff s ON s.id = gt.master_id
    ORDER BY gt.id ASC
    """,

    # ----------------------------------------------------------------
    # 22. Компоненты общего среднего чека для Metabase.
    # SUM(revenue) / COUNT(DISTINCT denominator_key) за выбранный период.
    # ----------------------------------------------------------------
    """
    CREATE OR REPLACE VIEW v_average_check_components AS
    WITH allowed_finance AS (
        SELECT ft.*
        FROM financial_transactions ft
        LEFT JOIN account_catalog acc
          ON acc.company_id = ft.company_id
         AND acc.account_id = ft.account_id
        WHERE ft.amount > 0
          AND LOWER(COALESCE(acc.title, '')) NOT LIKE '%бонус%'
          AND LOWER(COALESCE(acc.title, '')) NOT LIKE '%скид%'
          AND LOWER(COALESCE(acc.title, '')) NOT LIKE '%лояльн%'
          AND LOWER(COALESCE(acc.title, '')) NOT LIKE '%сертификат%'
    ),
    income AS (
        SELECT
            ft.company_id,
            ft.date::date AS event_date,
            CASE
                WHEN ft.sold_item_type = 'service' THEN 'service_revenue'
                WHEN ft.sold_item_type = 'goods_transaction' THEN 'goods_revenue'
                WHEN ft.sold_item_type IN ('client_account', 'personal_account', 'account_replenishment')
                  OR LOWER(COALESCE(ft.expense_title, '')) LIKE '%пополн%'
                  OR LOWER(COALESCE(ft.expense_title, '')) LIKE '%личн%'
                  OR LOWER(COALESCE(ft.expense_title, '')) LIKE '%депозит%'
                THEN 'topup_revenue'
                ELSE 'unclassified_income'
            END AS component,
            CASE
                WHEN ft.sold_item_type IN ('service', 'goods_transaction')
                  OR ft.sold_item_type IN ('client_account', 'personal_account', 'account_replenishment')
                  OR LOWER(COALESCE(ft.expense_title, '')) LIKE '%пополн%'
                  OR LOWER(COALESCE(ft.expense_title, '')) LIKE '%личн%'
                  OR LOWER(COALESCE(ft.expense_title, '')) LIKE '%депозит%'
                THEN ft.amount ELSE 0
            END AS revenue,
            NULL::text AS denominator_key,
            ft.id AS source_id
        FROM allowed_finance ft
        LEFT JOIN appointments a ON a.id = ft.record_id
        WHERE COALESCE(ft.sold_item_type, '') <> 'service' OR a.attendance = 1
    ),
    visit_denominator AS (
        SELECT
            a.company_id,
            a.date::date AS event_date,
            CASE WHEN a.client_id IS NULL
                THEN 'appointment_without_client'
                ELSE 'unique_client'
            END AS component,
            0::double precision AS revenue,
            CASE WHEN a.client_id IS NULL
                THEN 'appointment:' || a.company_id || ':' || a.id
                ELSE 'client:' || a.client_id
            END AS denominator_key,
            a.id AS source_id
        FROM appointments a
        WHERE a.attendance = 1
    ),
    goods_denominator AS (
        SELECT DISTINCT
            gt.company_id,
            gt.date::date AS event_date,
            'goods_check'::text AS component,
            0::double precision AS revenue,
            'goods:' || gt.company_id || ':' || gt.document_id AS denominator_key,
            gt.document_id AS source_id
        FROM goods_transactions gt
        WHERE gt.type_id = 1
          AND gt.document_id IS NOT NULL
    )
    SELECT * FROM income
    UNION ALL
    SELECT * FROM visit_denominator
    UNION ALL
    SELECT * FROM goods_denominator
    """,
]

LEGACY_VIEWS = [
    "v_loyalty_summary",
    "v_certificates_stats",
]


def refresh_analytics_views(verbose: bool = True):
    if verbose:
        print("Создание аналитических представлений в PostgreSQL...")
    database = init_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)

    if not database.test_connection():
        return {'success': False, 'created_count': 0, 'failed_views': ['connection']}

    created_count = 0
    failed_views = []

    with database.engine.connect() as conn:
        for view_name in LEGACY_VIEWS:
            conn.execute(text(f"DROP VIEW IF EXISTS {view_name}"))
        conn.commit()

        for i, view_sql in enumerate(VIEWS, 1):
            try:
                conn.execute(text(view_sql))
                conn.commit()
                if verbose:
                    print(f"  ✓ View {i}/{len(VIEWS)} создан")
                created_count += 1
            except Exception as e:
                if verbose:
                    print(f"  ✗ Ошибка при создании view {i}: {e}")
                conn.rollback()
                failed_views.append(i)

    if verbose:
        print(f"\n✓ Успешно создано {created_count} из {len(VIEWS)} аналитических представлений:")
        print("  - v_revenue_daily            — выручка по дням")
        print("  - v_revenue_by_staff         — выручка по сотрудникам")
        print("  - v_popular_services         — популярные услуги")
        print("  - v_staff_workload           — загрузка сотрудников")
        print("  - v_client_analytics         — клиентская аналитика и сегментация")
        print("  - v_revenue_monthly          — выручка по месяцам")
        print("  - v_attendance_stats         — конверсия записей (посещаемость)")
        print("  - v_finance_daily            — финансовый поток по дням")
        print("  - v_finance_by_account       — финансы по кассам")
        print("  - v_finance_monthly          — финансы по месяцам")
        print("  - v_goods_sales              — продажи товаров")
        print("  - v_goods_movement           — движение товаров по складам")
        print("  - v_staff_reviews            — рейтинг сотрудников по отзывам")
        print("  - v_reviews_monthly          — отзывы по месяцам")
        print("  - v_schedule_utilization     — загрузка расписания")
        print("  - v_companies_lookup         — справочник филиалов")
        print("  - v_services_lookup          — справочник услуг")
        print("  - v_calendar                 — BI-календарь: месяцы, недели, кварталы")
        print("  - v_appointments_enriched    — записи с человекочитаемыми полями")
        print("  - v_financial_transactions_enriched — финансы с календарём и названиями")
        print("  - v_goods_transactions_enriched     — товары с названиями операций")
        if failed_views:
            print(f"\n✗ Ошибки были в view: {', '.join(map(str, failed_views))}")

    return {
        'success': not failed_views,
        'created_count': created_count,
        'failed_views': failed_views,
    }


def main():
    refresh_analytics_views(verbose=True)


if __name__ == "__main__":
    main()
