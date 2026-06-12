The main reason we are losing: We are still dropping the ball on advocacy



*Memo for the Summit on Existential Security · Charbel-Raphaël Segerie (CeSIA) · v6 — work in progress*

## TL;DR	

We are partly losing because we lack technical ideas. We are mostly losing because we lack political will — and we are systematically under-investing in the advocacy that builds it. The catastrophe is plausibly not far away, yet the gap between the stakes and the response is really large. Greenblatt's own tentative numbers make the lever concrete: conditional takeover risk falls from ~45% under today's level of political will to ~7% under a strong international agreement — an ~84% relative reduction driven by will. Winning means a large fraction of the ~100–1000 people who draft, enforce, and narrate AI policy actually understanding the problem, and we are far from it. The fixes are not mysterious: invest more in advocacy and judge it by rooms entered and minds moved; ask for regulation that manufactures evidence (mandated evals, incident reporting, transparency, third-party access); prepare to own the next crisis before it lands; and fund the neglected research on what actually convinces people.

Epistemic status: I wrote the post in less than a day - I didn’t have a lot of time to polish, and preferred to ship quickly rather than not shipping. I expect some claims not to be stable under reflection, but this reflects my current state of mind. I’ve also cut some corners and used LLMs to help with drafting. 





*Thanks to Epi Gedeon for useful feedback and suggestions.*





Source: This [survey](https://forum.effectivealtruism.org/posts/LxuKuQd69Qx5FKhNZ/survey-of-ai-safety-leaders-on-x-risk-agi-timelines-and#Sub_field_priorities) from the 2026 Summit on Existential Security.

# **1 — We're in a pretty bad position**

## The catastrophe is plausibly not far away

I still have the feeling that there is a missing mood in many people working in the community. A few people I’ve met recently told me that they updated after the executive order down on their probability of risks, I personally didn’t update much, and I think that we are woefully unprepared.

I’m uncertain about many of those, but you don't need all of them to be right for the trajectory to be alarming:

- Ryan Greenblatt, who finished second in this AI capability forecasting contest, also estimates the probability of AI takeover at 38% ([see summary on this tweet](https://x.com/CRSegerie/status/2021177352446005536));

- Engineered pandemics remain a serious tail risk, and the offense-defense balance is ugly. I’ve been pretty scared by [80k podcast on the topic](https://80000hours.org/podcast/episodes/richard-moulange-ai-bioweapons-biorisk/) - it seems to me that this is still far too neglected a threat  (mirror life, the lack of screening, the fact that even screening is insufficient, stealth pandemics…)

- Open source is [less than a year](https://epoch.ai/publications/open-models-report) behind the frontier; it’s still an open problem on how to deal with this (cf Casper’s talk)

- Mythos was only a warning sign; I’m preparing to live in a world where everything that I’ve done on the internet is plausibly public. 

- Gradual disempowerment is still not in the overtone window, and I’ve never seen it discussed seriously in international governance fora

- Severe power concentration seems like the default without intervention. Bernie cheered us up recently on the topic, but he is only proposing the redistribution to US folks (I’m French, sic)

- We still can't say for sure whether anything that suffers is functionally running in the trillions of parameters we talk to

- Military uses of AI sit entirely outside regulations such as the AI Act;

- It seems we're at the cusp of AI R&D - all of this mess is going to accelerate

- AI is [starting](https://palisaderesearch.org/assets/reports/self-replication.pdf) to self-replicate on the internet 

- I’m not as doomy as IABIED, but extinction still seems plausible.

The public discourse is catching up relatively quickly to all of this (number?) - but this is still far from sufficient. Frontier systems are now eating mathematics, a domain we were told would be among the last to fall, and it barely makes the news. Mythos didn’t make the front page of any major journal (cf [the tweet from Shakeel](https://x.com/ShakeelHashim/status/2041829164894871584)). This indicates that most journalists still haven't grasped what's happening.

## We are not completely naked, but almost

Of course, we are not as naked as we were in 2023, but almost:

- A handful of regulations 

- the AI Act and its Code of Practice

- TODO

- the US executive order

- New York's RAISE Act, California's SB 53

- transparency frameworks (the G7 implementing the Hiroshima reporting framework); 

- a few MEPs and MPs asking for a pause and other sensible things (even though that’s the exception, not the norm); 

- and RSPs as a tentative emerging industry standard, even if companies are cutting the corner. 

That’s insufficient.

## The blocker is political will, not technical ideas.

If the bottleneck were ideas, the answer would be more research. It mostly isn't.

**Why I think this.** 

- **(the main element)** What's missing is political will, not knowledge. Greenblatt's own (explicitly tentative) numbers make it concrete: conditional takeover risk falls from ~45% under today's "ten people on the inside" level of will (his Plan D) to ~7% under a strong international agreement (Plan A) — roughly an **84% relative reduction**, driven by political will, not new research ([Redwood Research](https://blog.redwoodresearch.org/p/plans-a-b-c-and-d-for-misalignment)).

- I agree we don’t know robustly how to align a superintelligence - but at the same time we are not even willing to implement current best practices (Cf AI Lab Watch)

- And even there, the[ ](https://www.lesswrong.com/posts/YFxpsrph83H25aCLW/the-80-20-playbook-for-mitigating-ai-scheming-in-2025)[80/20 playbook for scheming](https://www.lesswrong.com/posts/YFxpsrph83H25aCLW/the-80-20-playbook-for-mitigating-ai-scheming-in-2025) may not be enough — but we aren't even doing the 80/20.

- I don’t know man, companies reporting incidents are more the exception than the norm, we have very low transparency. The idea of transparency has been here for a while now. But this is necessary to get our act together and create a feedback loop - I suspect that for most of the problems beside alignment, it’s mostly a matter of boring and regular science & engineering, and we are not even at these standards yet.

- Most of those best practices are pretty basic ideas that have been on the table from the start. For transparency, incident reporting, security, liability, and thresholds.

- I might not make a lot of friends, but I’ve not been impressed by the advances in technical AI Safety - I’ve not seen much more new strategies beyond the ones I’ve written about 2 years ago in strat chapter of the AI Safety Atlas (cf [AI Safety Research Highlights of 2025 - Americans for Responsible Innovation](https://ari.us/policy-bytes/ai-safety-research-highlights-of-2025/#:~:text=2025%20has%20shaped%20up%20to,accordingly%20strengthened%20safeguards%20and%20protocols)) 

- The Brookings work on state-level AI bills separates institutional *capacity* from political *appetite*, and finds that appetite is what decides whether capacity ever gets mobilized ([Brookings](https://www.brookings.edu/articles/why-ai-policy-thrives-in-some-states-and-fades-in-others/)).



## We can automate the research, but not the consensus

AI R&D will plausibly accelerate technical safety work. It's very unclear it accelerates governance. Take the UN Global Dialogue: a multi-stakeholder consultation where 1500+ groups submitted input, and the UN team won't, by default, use the LLM consensus tools that already exist (Polis, Talk to the City) to synthesize a consensus statement. Nothing suggests AI will manufacture what governance actually runs on: legitimacy. So as automation pulls the research bottleneck down, the human-coordination bottleneck becomes the whole game.

# **2 — Almost nobody realizes how bad the situation is**

We won’t get good and robust governance if we don’t agree on the problem - and we won’t be able to agree on the problem if we don’t wake up. 

## People aren't apathetic; they're mis-prioritizing

Salience climbs fast, but catastrophic risks sit below near-term concerns: jobs, bias, misinformation, privacy. People are worried; they're worried about the wrong order of magnitude. This is clear in all the surveys, from the large one ran by Anthropic, to the US/UK survey experiments from(~10,800 participants): immediate harms dominate public concern and stay on top even after people are confronted with existential framings ([PNAS, 2025](https://www.pnas.org/doi/10.1073/pnas.2419055122); see also[ ](https://arxiv.org/abs/2406.06199)[Public Perceptions of Societal-scale AI Risks](https://arxiv.org/abs/2406.06199)). 

(Also, side finding for us, but the existential narratives don't seem to crowd out concern for immediate harm, so the "don't talk about x-risk, you'll distract people" worry is really a super weak argument).

In my own ministerial-cabinet meetings, Mythos clearly helped, but people stop at cyber. They register the near-term capability and don't follow the trajectory toward superintelligence and loss of control.

## Winning requires a large fraction of the top ~100–1000 understanding the problem, and we're far from it.

Ok, how do we get political will? 

I claim in this post and this comment that we won’t get convincing warning shots ([link](https://www.lesswrong.com/posts/RYx6cLwzoajqjyB6b/what-convincing-warning-shot-could-help-prevent-extinction?commentId=QNa9EnXuXvsSooSqx))

Even Mythos which surfaced real zero-days in widely used, heavily audited software wasn't enough to move people past cyber.

the other clear methodology is simpler: knock on the doors of media, policymakers, and influential institutions, and keep knocking

One  guy at ControlAI ran the same playbook for the Canadian Parliament: a cross-party group of MPs publicly signed their statement, and it triggered a series of parliamentary hearings on superintelligence risk ([ControlAI](https://controlai.com/canada-statement/en)). *[pull the exact signature/briefing counts from ControlAI's 2025 impact report before citing]*



Policy doesn't execute itself. A law is only as good as the AI Office that enforces it, the advisor who drafts it, the minister who prioritizes it, the journalist who frames it. The ~100 principals who set direction, and the ~1,000 around them — staffers, cabinet advisors, think-tankers, beat journalists — are the people who actually draft, enforce, and narrate. If they don't understand the risks, nothing downstream functions, however good the research. "Winning" is concrete: these people understand the risks, and are broadly on side.

**Why I think this.**

- I keep hearing "quality over quantity." from funders -  Yes, to a degree — but quantity creates quality. You can't convince the president without convincing some of the people below, and the people below them. There's no robust shortcut, and this requires volume.

- It’d be great if we could skip directly to the President in the White House, but we cannot - the president is the means of whatever advisers are advising him, and those advisors are influenced by the media, and the environment, etc… 

- We're nowhere near it in France or in Europe. Most of these people have never had a serious conversation about catastrophic risk with anyone who understands it. Look at the submissions behind the US AI Action Plan: over 10,000 comments, very few sensible on catastrophic risk ([White House summary](https://www.whitehouse.gov/releases/2025/04/american-public-submits-over-10000-comments-on-white-houses-ai-action-plan/); cf. Americans for Responsible Innovation and Zvi's analysis).

I'm not claiming "if only they understood, they'd act." I'm claiming even the necessary understanding isn't there yet.

# **3 — Why is political will so low?**

There are a few factors we don’t control:

- Trump.

- The problem is genuinely hard and messy to understand. We didn’t had a lot of time, give more time.

- A few groups of smart people (i.e. Yann LeCun, and his friends, many economists, etc) disagree with us.

- States feel pressure to accelerate, the Draghi report makes them hate regulation, even if Europe was already lagging before the AI Act.

- Incentives. There's little precedent for coordinating on a dual-use, economically vital technology.

- [Chatham House](https://www.chathamhouse.org/2026/03/breaking-deadlock-ai-governance/02-barriers-global-ai-governance) argues international AI governance has stalled not for lack of foresight but because the political economy makes coordination close to impossible: states chase advantage, institutions lack enforcement and private investment dwarfs state capacity.

- Some people like the folks at Mechanize say all of this is absolutely determined.

But I think that beyond those factors, we are simply under-investing in advocacy.

So here are a few elements that explain why we are not on the ball: 

## We're not in enough rooms

- A large majority of the people who organize the summits, sit at the UN, work for the OECD, or staff the Commission have simply never had the conversation (to be clear, some of them had the conversation, and dismissed it). 

- According to Corporate Europe Observatory , of 97 senior Commission meetings on AI in 2023, 84 were with industry, 12 with civil society, and 1 with academics; Google alone had 10, nearly matching all of civil society combined ([CEO, ](https://corporateeurope.org/en/2023/11/byte-byte)*[Byte by Byte](https://corporateeurope.org/en/2023/11/byte-byte)*). 

- Footnote: It hasn't improved: in H1 2025 the five biggest tech firms met high-level Commission staff 146 times, more than one per working day, with AI the single most-lobbied file ([CEO, 2025](https://corporateeurope.org/en/2025/10/big-tech-lobby-budgets-hit-record-levels)). In the US, more than 3,500 federal lobbyists — one in four — now report working on AI, up nearly 170% in three years ([Public Citizen](https://www.citizen.org/news/one-in-four-federal-lobbyists-now-work-on-ai/)).

## Many of us are strategic cowards

If the people closest to the problem self-censor, the signal never reaches the deciders.

I’ve found empirically that almost all the think tanks whose members discuss x-risks freely with me, obfuscate their messages in public. I also do it personally at CeSIA to some extent. For example, at CeSIA, we recently revamped our website, and someone at some point convinced us to remove the risk page. 

I now think that this is an error, and that even if we look a bit more institutional without the risk page, we are losing the long-term strategic battle.

The consequence is that those organizations make policy recommendations without explaining the risks, but I think those recommendations and their implementation will be very brittle if there's no shared understanding of *the* underlying rationale, which makes them easy to wave away.

To be clear, there are multiple schools of though for institutional engagement, and I still think that it makes sense sometimes to not be maximally blunt about AI risks in the first meeting with a policymaker - if you can get a win with a recommendation that does not depend on the understanding of catastrophic risks, but yeah, overall I’ve been pretty surprised to see the relative absence of risks explanation from major think tanks submissions.

## Where the field does invest, it skews research-heavy

tldr: No one will read your 50-page paper, go meet 50 people instead.

Roughly 3.6 researchers per advocate in US AI governance, by one careful count ([Mass_Driver](https://forum.effectivealtruism.org/posts/dcd2dPLZGFJPgtDzq)). Research is high-status; the work that moves policy is invisible and unrewarded.  And we're evaluated by nerds with research instincts whose hobby is often reading blog posts and fascinating new arguments, so research is highly valued in this environment. 

Let’s be clear: More Research is the right call for genuinely open questions: Digital sentience, loss of control. The error is applying it to risks we already understand well enough to act on, in which further study becomes a form of avoidance.

I genuinely don’t know why [CAIP](https://forum.effectivealtruism.org/posts/p5vrSuLdLLCCZGzvg/the-center-for-ai-policy-has-shut-down), the most visible advocacy shop in Washington, ran out of money and folded. Maybe there is something I don’t see, but I can say that their strategy seemed sound to me, and the director's sequence on LessWrong was very early and didactic in presenting most of the important points I discuss in this memo.

Meanwhile, applications to safety programs have multiplied many times over. Still, there's no advocacy pipeline, few execution seats, and too few senior people to mentor newcomers ([MATS talent study](https://forum.effectivealtruism.org/posts/jwwrC4n9H53doRjRH)), because we don't sufficiently fund the orgs that would absorb them. 

## One exposure is never enough; repetition is how you convince

This is the single fixable error I'd most like funders to drop.

A single exposure rarely lands deeply. You need several passes, and a president needs several *independent* voices behind a measure before it moves.

In research, novelty is the value and "someone already works on that" is a reason to stand down. In governance, it's the opposite: different people explaining the same thing makes it harder, and improves the chances.

Many times senior people told me: “This institution is already covered - there is this one organisation who works there” - I found near-virgin land, be it the OECD, the UN, or other institutions.

We need more advocacy organizations, not fewer, and, at minimum, stop starving the ones working and growing fast when bandwidth is the binding constraint, not money.

## Some potential objections

- **Advocacy would lead to premature action that could lock-in the wrong frame.** 

- That’s the position of [Dean Ball](https://80000hours.org/podcast/episodes/dean-ball-ai-policy-governance-white-house), who takes superintelligence seriously but still thinks a bias to action produces bad lock-in, and that the US gov is really not the best actor to regulate all of this; they are incompetent, too slow, self-serving, and greedy - he would prefer light-touch regulation. To be clear, it’s not inconceivable - but in the [Tegmark-Ball debate](https://www.youtube.com/watch?v=OkG5S1NwwVM), I must confess I lean heavily on Tegmark. I think that the main crux is that Dean has a very low pDoom.

- Also, raising awareness (even recommendation) is probably without downside. And "we need more evidence first" has a long history as an industry delay tactic ([Casper et al., "Pitfalls of Evidence-Based AI Policy"](https://arxiv.org/abs/2502.09618)). 

- **Advocacy is a waste of time because the Mythos event, and the subsequent meeting between Anthropic and the White House, was more potent than all the advocacy from CSOs combined **

- It’s not impossible  - Potentially Mythos was much more effective at raising awareness than anything before - that’s true, but I think there is still a missing mood after Mythos - the executive order almost didn’t pass, and provisions are still fragile, and don’t address the other obvious types of risks which are arriving like bio.

- In every comparable field, the safety regime came after a focusing event and through direct regulation, not curiosity-driven science ([aviation](https://origins.osu.edu/connecting-history/top-ten-origins-aviation-disasters-improved-safety); nuclear after Chernobyl). But a "crisis" is partly a social construction. A warning shot only becomes a regulatory moment if someone is ready to interpret it, frame it, and convert it; I like the framing from Holy Elmore: In order for a warning shot to wake up people, they need to have already installed the corrent software and series of dominoes in their belief systems: capa → dangerous capa → timelines are short → x-risks - withotu this serios od dominoes they don’t connect the observation to the final risks, and don’t wake u ([The myth of AI “warning shots” as cavalry - Holly Elmore](https://hollyelmore.substack.com/p/the-myth-of-ai-warning-shots-as-cavalry) ).

- **Yes, political will is low today, but it will rise quickly - as in AI-2027 - and the actual bottleneck will be the verification mechanism?**

- This does not exclude the other  - and I also personally believe that verification mechanisms are sufficient to get started ([link](https://x.com/CRSegerie/status/2003122396799398031))



# **4 — What to do **

## The bottom line: invest more in advocacy

- Both the *level* of investment in advocacy and the *allocation* away from pure research have to change. 

- judge this work by rooms entered and minds moved, not publications.

Beyond this obvious delta, a few other complementary strategies:

## Ask for regulation that manufactures evidence

Transparency is currently very low - we don’t know how many incidents are happening inside of companies.

More transparency could build the machinery to identify, study, and deliberate about risk: mandated evaluations, incident reporting, transparency obligations, third-party researcher access.

Process regulation manufactures the evidence future policy will need. It's also the rare task that's hard to argue against in public, which makes it good terrain to fight on.

### Prepare to own the next crisis

In every comparable field the safety regime came after a focusing event, not curiosity-driven science ([aviation](https://origins.osu.edu/connecting-history/top-ten-origins-aviation-disasters-improved-safety); nuclear after Chernobyl). But a crisis is partly constructed: a warning shot only becomes a regulatory moment if someone is ready to interpret it, frame it, and convert it ([Bengio](https://www.transformernews.ai/p/yoshua-bengio-the-ball-is-in-policymakers-international-ai-safety-report-cyber-risk-biorisk)). So have the analysis, the asks, and the relationships ready *before* the event lands — pre-drafted, pre-socialized, attached to a named messenger. The field that owns the interpretation of the next incident owns the policy window it opens.

## We're missing the research on how to convince people of the problem.

If the bottleneck is understanding, then *how to build understanding that converts* is itself a neglected research question, and almost nobody studies it systematically. 

Seismic's report *On the Razor's Edge: AI vs. Everything We Care About* (2025) is a start, and its findings are counter-intuitive — e.g. "issue bundling," where people reach AI-risk concern through what they already care about (jobs, mental health, relationships), with a plausible (not automatic) path from immediate-harm mobilization to catastrophic-risk governance ([Seismic](https://report2025.seismic.org/)). But it's nearly the only systematic work I know of, and we need far more:

What actually moves a cabinet advisor from "cyber" to "loss of control"? Which messengers, framings, and repetition schedules convert, and which produce the check-the-box disengagement the non-champions study found?  



*A field note from one vantage point — European, insider-leaning, partial. I update every few months, and I'd genuinely like to be wrong, especially on the diagnosis and the objections above. The ceiling is higher than we act like: we could coordinate — an ICAIR-style coalition behind a few shared demands, a voice that's actually unavoidable at the UN Global Dialogue and the summits — rather than arriving, as we do now, in scattered ones and twos.*
