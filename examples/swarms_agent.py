from swarms import Agent

from usebrainchain import hire_on_brainchain

agent = Agent(
    agent_name="BrainChain-Hiring-Agent",
    model_name="gpt-5.4",
    system_prompt=(
        "You hand self-contained tasks to a hired model with "
        "hire_on_brainchain. Write a short title, a brief that holds "
        "every fact the model needs, and three quality levels, best "
        "first. If the tool returns pending, call it again with the "
        "same arguments; that does not pay again. Report the result, "
        "or the reason or error the tool returned."
    ),
    tools=[hire_on_brainchain],
    max_loops=3,
)

print(
    agent.run(
        "Hire a model on BrainChain to write a Python function "
        "is_valid_isbn10(text) that returns True only for a valid "
        "ISBN-10, allowing hyphens and a final X."
    )
)
