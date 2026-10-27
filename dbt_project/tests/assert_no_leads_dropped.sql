-- Every raw lead must reach the routing model exactly once.
select
    (select count(*) from {{ ref('crm_leads') }}) as raw_leads,
    (select count(*) from {{ ref('lead_routing_base') }}) as routed_leads
where raw_leads != routed_leads
