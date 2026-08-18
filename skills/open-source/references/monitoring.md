# External Instrumentation and Cost Tracking

Browser Use does not install or initialize a tracking or tracing backend. Applications
that need traces can wrap agent calls with their own instrumentation library without
changing Browser Use configuration.

## Cost tracking

Enable local token and cost accounting on an agent:

```python
agent = Agent(task="...", llm=llm, calculate_cost=True)
history = await agent.run()
print(history.usage)
```

## Application-owned instrumentation

Keep provider initialization and span ownership in application code:

```python
with application_tracer.start_as_current_span("browser task"):
    history = await agent.run()
```

Zero-code instrumentation such as OpenLIT may also be installed and configured by the
application. Browser Use neither imports it nor sends run events to it.
