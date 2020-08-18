# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

import funsor

from pyro.distributions.util import copy_docs_from
from pyro.infer import ELBO
from pyro.infer import TraceEnum_ELBO as OrigTraceEnum_ELBO
from pyro.poutine.messenger import Messenger
from pyro.poutine.util import prune_subsample_sites

from pyro.contrib.funsor import to_data, to_funsor
from pyro.contrib.funsor.handlers import enum, plate, replay, trace


def terms_from_trace(tr):
    """Helper function to extract elbo components from execution traces."""
    # data structure containing densities, measures, scales, and identification
    # of free variables as either product (plate) variables or sum (measure) variables
    terms = {"log_factors": [], "log_measures": [], "scale": to_funsor(1.),
             "plate_vars": frozenset(), "measure_vars": frozenset()}
    for name, node in prune_subsample_sites(tr).nodes.items():
        if node["type"] != "sample":
            continue
        # grab plate dimensions from the cond_indep_stack
        terms["plate_vars"] |= frozenset(f.name for f in node["cond_indep_stack"] if f.vectorized)
        # grab the log-density, found at all sites except those that are not replayed
        if node["is_observed"] or not node.get("replay_skipped", False):
            terms["log_factors"].append(node["funsor"]["log_prob"])
        # grab the log-measure, found only at sites that are not replayed or observed
        if node["funsor"].get("log_measure", None) is not None:
            terms["log_measures"].append(node["funsor"]["log_measure"])
            # sum variables: the fresh non-plate variables at a site
            terms["measure_vars"] |= frozenset(node["funsor"]["value"].inputs) | frozenset([name]) - terms["plate_vars"]
        # grab the scale, assuming a common subsampling scale
        # cases for scale:
        # 1. model site that depends on enumerated variable: common scale
        # 2. model site that does not depend on enumerated variable: default
        # 3. all guide sites: default
        if node.get("replay_active", False) and set(node["funsor"]["log_prob"].inputs) - terms["plate_vars"] - {name}:
            # model site that depends on enumerated variable: common scale...
            terms["scale"] = node["funsor"]["scale"]
        else:  # default scale behavior
            node["funsor"]["log_prob"] *= node["funsor"]["scale"]
    return terms


@copy_docs_from(OrigTraceEnum_ELBO)
class TraceEnum_ELBO(ELBO):

    def _get_trace(self, *args, **kwargs):
        raise ValueError("shouldn't be here")

    def differentiable_loss(self, model, guide, *args, **kwargs):

        # get batched, enumerated, to_funsor-ed traces from the guide and model
        with plate(size=self.num_particles) if self.num_particles > 1 else Messenger(), \
                enum(first_available_dim=(-self.max_plate_nesting-1) if self.max_plate_nesting else None):
            guide_tr = trace(guide).get_trace(*args, **kwargs)
            model_tr = trace(replay(model, trace=guide_tr)).get_trace(*args, **kwargs)

        # extract from traces all metadata that we will need to compute the elbo
        guide_terms = terms_from_trace(guide_tr)
        model_terms = terms_from_trace(model_tr)

        # build up a lazy expression for the elbo
        with funsor.interpreter.interpretation(funsor.terms.lazy):
            # identify and contract out auxiliary variables in the model with partial_sum_product
            contracted_factors, uncontracted_factors = [], []
            for f in model_terms["log_factors"]:
                if model_terms["measure_vars"].intersection(f.inputs):
                    contracted_factors.append(f)
                else:
                    uncontracted_factors.append(f)
            model_terms["log_factors"] = funsor.sum_product.partial_sum_product(
                funsor.ops.logaddexp, funsor.ops.add, model_terms["log_measures"] + contracted_factors,
                plates=model_terms["plate_vars"], eliminate=model_terms["measure_vars"]
            )

            # correctly incorporate the effects of subsampling and handlers.scale,
            # and collect the individual elbo cost terms (logp, -logq)
            costs = [model_terms["scale"] * f for f in model_terms["log_factors"]]
            costs += uncontracted_factors
            costs += [-f for f in guide_terms["log_factors"]]

            # finally, integrate out guide variables in the elbo and all plates
            plate_vars = guide_terms["plate_vars"] | model_terms["plate_vars"]
            elbo = to_funsor(0, output=funsor.reals())
            for cost in costs:
                log_prob = funsor.sum_product.sum_product(
                    funsor.ops.logaddexp, funsor.ops.add,
                    guide_terms["log_measures"],
                    plates=plate_vars,
                    eliminate=(plate_vars | guide_terms["measure_vars"]) - frozenset(cost.inputs)
                )
                elbo_term = funsor.Integrate(log_prob, cost, guide_terms["measure_vars"] & frozenset(cost.inputs))
                elbo_term = elbo_term.reduce(funsor.ops.add, plate_vars & frozenset(cost.inputs))
                elbo += elbo_term

        # evaluate the elbo, using memoize to share tensor computation where possible
        with funsor.memoize.memoize():
            return -to_data(funsor.optimizer.apply_optimizer(elbo))