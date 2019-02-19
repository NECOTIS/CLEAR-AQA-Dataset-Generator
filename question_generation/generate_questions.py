# TODO : Add author mention
# TODO : Change heading comments
# FIXME : A lot of info are retrieved from metadata. They don't change for each instantiations. We should only retrieve them once instead of everytime
# FIXME : Break down this file in multiple files

# Copyright 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree. An additional grant
# of patent rights can be found in the PATENTS file in the same directory.

from __future__ import print_function
import argparse, ujson, os, random, math, statistics
import time
import re
import sys
from shutil import rmtree as rm_dir
from functools import reduce
from itertools import groupby
import copy
import numpy as np
from utils.misc import init_random_seed
from question_generation.helper import  question_node_shallow_copy, placeholders_to_attribute, \
                                        translate_can_be_null_attributes, replace_optionals, \
                                        write_questions_part_to_file

from collections import OrderedDict

import question_engine as qeng

# FIXME : Remove cprofile args
# FIXME : Remove args.time_DFS
# FIXME : Update this documentation string
"""
Generate synthetic questions and answers for CLEVR images. Input is a single
JSON file containing ground-truth scene information for all images, and output
is a single JSON file containing all generated questions, answers, and programs.

Questions are generated by expanding templates. Each template contains a single
program template and one or more text templates, both with the same set of typed
slots; by convention <Z> = Size, <C> = Color, <M> = Material, <S> = Shape.

Program templates may contain special nodes that expand into multiple functions
during instantiation; for example a "filter" node in a program template will
expand into a combination of "filter_size", "filter_color", "filter_material",
and "filter_shape" nodes after instantiation, and a "filter_unique" node in a
template will expand into some combination of filtering nodes followed by a
"unique" node.

Templates are instantiated using depth-first search; we are looking for template
instantiations where (1) each "unique" node actually refers to a single object,
(2) constraints in the template are satisfied, and (3) the answer to the question
passes our rejection sampling heuristics.

To efficiently handle (1) and (2), we keep track of partial evaluations of the
program during each step of template expansion. This together with the use of
composite nodes in program templates (filter_unique, relate_filter_unique) allow
us to efficiently prune the search space and terminate early when we know that
(1) or (2) will be violated.
"""



parser = argparse.ArgumentParser(fromfile_prefix_chars='@')

parser.add_argument('--output_folder', default='./output',
    help="Folder where to store the generated questions")
parser.add_argument('--output_filename_prefix', default='CLEAR',
    help="Prefix for the output file")
parser.add_argument('--output_version_nb', default='0.0.1',
    help="Identifier of the dataset version.")
parser.add_argument('--set_type', default='train', type=str,
                    help="Specify the set type (train/val/test)")

# TODO : Change defaults values for arguments
# TODO : Change argument text
# Inputs
parser.add_argument('--metadata_file', default='metadata.json',
    help="JSON file containing metadata about functions")
parser.add_argument('--synonyms_json', default='synonyms.json',
    help="JSON file defining synonyms for parameter values")
parser.add_argument('--template_dir', default='CLEVR_1.0_templates',
    help="Directory containing JSON templates for questions")

# Output
parser.add_argument('--write_to_file_every',
    default=5000, type=int,
    help="The number of questions that will be written to each files.")

# Control which and how many images to process
parser.add_argument('--scene_start_idx', default=0, type=int,
    help="The image at which to start generating questions; this allows " +
         "question generation to be split across many workers")
parser.add_argument('--num_scenes', default=0, type=int,
    help="The number of images for which to generate questions. Setting to 0 " +
         "generates questions for all scenes in the input file starting from " +
         "--scene_start_idx")

# Control the number of questions per image; we will attempt to generate
# templates_per_image * instances_per_template questions per image.
parser.add_argument('--templates_per_image', default=10, type=int,
    help="The number of different templates that should be instantiated " +
         "on each image")
parser.add_argument('--instances_per_template', default=1, type=int,
    help="The number of times each template should be instantiated on an image")
parser.add_argument('--instantiation_retry_threshold', default=10000, type=int,
    help="Maximum number of retry attempt in order to reach the instances_per_template")


# Misc
parser.add_argument('--random_nb_generator_seed', default=None, type=int,
    help='Set the random number generator seed to reproduce results')
parser.add_argument('--reset_counts_every', default=250, type=int,
    help="How often to reset template and answer counts. Higher values will " +
         "result in flatter distributions over templates and answers, but " +
         "will result in longer runtimes.")
parser.add_argument('--verbose', action='store_true',
    help="Print more verbose output")
parser.add_argument('--time_dfs', action='store_true',
    help="Time each depth-first search; must be given with --verbose")
parser.add_argument('--profile', action='store_true',
    help="If given then run inside cProfile")
parser.add_argument('--clear_existing_files', action='store_true',
                    help='If set, will delete all files in the output folder before starting the generation.')
# args = parser.parse_args()


def precompute_filter_options(scene_struct, attr_keys, can_be_null_attributes):
  # Keys are tuples (size, color, shape, material) (where some may be None)
  # and values are lists of object idxs that match the filter criterion
  attribute_map = OrderedDict()

  # Precompute masks
  masks = []
  for i in range(2 ** len(attr_keys)):
    mask = []
    for j in range(len(attr_keys)):
      mask.append((i // (2 ** j)) % 2)
    masks.append(mask)

  np.random.shuffle(masks)

  for object_idx, obj in enumerate(scene_struct['objects']):
    key = qeng.get_filter_key(attr_keys, scene_struct, object_idx)

    for mask in masks:
      masked_key = []
      for a, b in zip(key, mask):
        if b == 1:
          masked_key.append(a)
        else:
          masked_key.append(None)
      masked_key = tuple(masked_key)
      if masked_key not in attribute_map:
        attribute_map[masked_key] = set()
      attribute_map[masked_key].add(object_idx)

  # Keep only filters with Null values for allowed attributes
  deleted_keys = set()
  for key in list(attribute_map.keys()):
    for i, val in enumerate(key):
      if val is None and attr_keys[i] not in can_be_null_attributes and key not in deleted_keys:
          deleted_keys.add(key)
          del attribute_map[key]

  # FIXME : Generalize this
  # FIXME : Make sure this will hold when there is more positional attributes
  # Removing position attribute if there is only one occurrence of the instrument
  if "position_instrument" in attr_keys:
    keys_by_instrument = OrderedDict()
    for instrument, key in groupby(attribute_map.keys(), lambda x: x[0]):
      if instrument not in keys_by_instrument:
        keys_by_instrument[instrument] = []
      keys_by_instrument[instrument] += list(key)

    for instrument, keys in keys_by_instrument.items():
      if len(keys) == 1:
        # Only have 1 object, we remove the position attribute
        attribute_map[(instrument, None)] = attribute_map[keys[0]]
        del attribute_map[keys[0]]

  if '_filter_options' not in scene_struct:
    scene_struct['_filter_options'] = {}

  scene_struct['_filter_options'][tuple(attr_keys)] = attribute_map


def find_filter_options(object_idxs, scene_struct, attr, can_be_null_attributes):
  # Keys are tuples (size, color, shape, material) (where some may be None)
  # and values are lists of object idxs that match the filter criterion
  filter_key = tuple(attr)

  if '_filter_options' not in scene_struct or filter_key not in scene_struct['_filter_options']:
    precompute_filter_options(scene_struct, attr, can_be_null_attributes)

  attribute_map = OrderedDict()
  object_idxs = set(object_idxs)
  for k, vs in scene_struct['_filter_options'][filter_key].items():
    attribute_map[k] = sorted(list(object_idxs & vs))
  return attribute_map


def add_empty_filter_options(attribute_map, metadata, can_be_null_attributes, attr_keys, num_to_add):
  '''
  Add some filtering criterion that do NOT correspond to objects
  '''

  attr_vals = []
  for key in attr_keys:
    vals = metadata['attributes'][key]['values']
    if key in can_be_null_attributes:
         vals.append(None)

    attr_vals.append(vals)

  attr_vals_len = list(map(lambda x: len(x), attr_vals))

  if len(attr_vals) > 1:
    max_nb_filter = reduce(lambda x, y: x * y, attr_vals_len)
  else:
    max_nb_filter = attr_vals_len[0]

  target_size = min(len(attribute_map) + num_to_add, max_nb_filter)

  while len(attribute_map) < target_size:
    k = tuple(random.choice(v) for v in attr_vals)
    if k not in attribute_map:
      attribute_map[k] = []


def find_relate_filter_options(object_idx, scene_struct, attr, can_be_null_attributes,
                               unique=False, include_zero=False, not_unique=False, trivial_frac=0.1):
  options = OrderedDict()

  attr = [a for a in attr if not a.startswith('relate')]
  filter_key = tuple(attr)

  if '_filter_options' not in scene_struct or filter_key not in scene_struct['_filter_options']:
    precompute_filter_options(scene_struct, attr, can_be_null_attributes)

  nb_filters = len(scene_struct['_filter_options'][filter_key].keys()) * len(scene_struct['relationships'])
  nb_trivial = int(round(nb_filters * trivial_frac / (1 - trivial_frac)))

  # TODO/VERIFY : Will probably have to change the definition of "trivial"
  # TODO_ORIG: Right now this is only looking for nontrivial combinations; in some
  # cases I may want to add trivial combinations, either where the intersection
  # is empty or where the intersection is equal to the filtering output.
  trivial_options_keys = []
  non_trivial_options_keys = []
  all_options = {}

  for relationship in scene_struct['relationships']:
    relationship_index = scene_struct['_relationships_indexes'][relationship['type']]
    related = set(scene_struct['relationships'][relationship_index]['indexes'][object_idx])
    if len(related) == 0:
      # If no relation, the object is the first (No before relations) or the last (No after relations)
      continue
    for filters, filtered in scene_struct['_filter_options'][filter_key].items():
      intersection = related & filtered
      trivial = (intersection == filtered)
      if unique and len(intersection) != 1:
        continue
      if not_unique and len(intersection) <= 1:
        continue
      if not include_zero and len(intersection) == 0:
        continue

      key = (relationship['type'], filters)
      if trivial:
        trivial_options_keys.append(key)
      else:
        non_trivial_options_keys.append(key)
      all_options[key] = sorted(list(intersection))

  np.random.shuffle(trivial_options_keys)
  options_to_keep = non_trivial_options_keys + trivial_options_keys[:nb_trivial]

  # FIXME : Looping a second time is really ineficient.. We do it to make sure that we keep the same order in the dict to ensure reproducibility
  for relationship in scene_struct['relationships']:
    for filters, filtered in scene_struct['_filter_options'][filter_key].items():
      key = (relationship['type'], filters)
      if key in options_to_keep:
        options[key] = all_options[key]

  return options


def validate_constraints(template, state, outputs, param_name_to_attribute, verbose):
  for constraint in template['constraints']:
    if constraint['type'] == 'NEQ':
      p1, p2 = constraint['params']
      v1, v2 = state['vals'].get(p1), state['vals'].get(p2)
      if v1 is not None and v2 is not None and v1 == v2:
        if verbose:
          print('skipping due to NEQ constraint')
          print(constraint)
          print(state['vals'])
        return False
    elif constraint['type'] == 'NULL':
      p = constraint['params'][0]
      p_type = param_name_to_attribute[p]
      v = state['vals'].get(p)
      if v is not None:
        skip = False
        if p_type == 'instrument' and v != 'thing': skip = True  # FIXME : Hardcoded stuff here
        if p_type != 'instrument' and v != '': skip = True
        if skip:
          if verbose:
            print('skipping due to NULL constraint')
            print(constraint)
            print(state['vals'])
          return False
    elif constraint['type'] == 'NOT_NULL':
      p = constraint['params'][0]
      p_type = param_name_to_attribute[p]
      v = state['vals'].get(p)
      if v is not None and (v == '' or v == 'thing'):
        skip = True  # FIXME : Ugly
        if skip:
          if verbose:
            print('skipping due to NOT NULL constraint')
            print(constraint)
            print(state['vals'])
          return False
    elif constraint['type'] == 'OUT_NEQ':
      i, j = constraint['params']
      i = state['input_map'].get(i, None)
      j = state['input_map'].get(j, None)
      if i is not None and j is not None and outputs[i] == outputs[j]:
        if verbose:
          print('skipping due to OUT_NEQ constraint')
        return False
    else:
      assert False, 'Unrecognized constraint type "%s"' % constraint['type']

  return True


bool_to_yes_no = ['no', 'yes']
def instantiate_texts_from_solutions(template, synonyms, final_states):
  # Actually instantiate the template with the solutions we've found
  text_questions, structured_questions, answers = [], [], []
  for state in final_states:
    structured_questions.append(state['nodes'])

    # Translating True/False values to yes/no   # FIXME : Should we translate them from the beginning instead ?
    if type(state['answer']) is bool:
      state['answer'] = bool_to_yes_no[state['answer']]

    answers.append(state['answer'])
    text = random.choice(template['text'])
    for name, val in state['vals'].items():
      if val in synonyms:
        val = random.choice(synonyms[val])
      text = text.replace(name, val)
      text = ' '.join(text.split())
    text = replace_optionals(text)
    text = ' '.join(text.split())
    text = other_heuristic(text, state['vals'])
    text_questions.append(text)

  return text_questions, structured_questions, answers


def instantiate_templates_dfs(scene_struct, template, metadata, answer_counts,
                              synonyms, max_instances=None, reset_threshold=0, verbose=False):

  param_name_to_attribute = placeholders_to_attribute(template['text'][0], metadata)

  if "_can_be_null_attributes" not in template:
    template['_can_be_null_attributes'] = translate_can_be_null_attributes(template['can_be_null_attributes'],
                                                                               param_name_to_attribute)
  # Initialisation
  states = []

  def reset_states_if_needed(current_states):
    if reset_states_if_needed.reset_counter < reset_threshold:
      if len(current_states) == 0:
        initial_state = {
          'nodes': [question_node_shallow_copy(template['nodes'][0])],
          'vals': {},
          'input_map': {0: 0},
          'next_template_node': 1,
        }
        current_states = [initial_state]
        reset_states_if_needed.reset_counter += 1
    else:
      if verbose: print("--> Retried %d times. Could only instantiate %d on %d. Giving up on this template" % (reset_threshold, len(final_states), max_instances))
      current_states = []

    return current_states

  # Instantiate a counter to keep track of the number of reset
  reset_states_if_needed.reset_counter = -1

  states = reset_states_if_needed(states)
  final_states = []
  while states:
    state = states.pop()

    # Check to make sure the current state is valid
    q = {'nodes': state['nodes']}
    outputs = qeng.answer_question(q, metadata, scene_struct, all_outputs=True)
    answer = outputs[-1]
    if answer == '__INVALID__':
      if verbose: print("Skipping due to invalid answer")
      states = reset_states_if_needed(states)
      continue

    if not validate_constraints(template, state, outputs, param_name_to_attribute, verbose):
      states = reset_states_if_needed(states)
      continue

    # We have already checked to make sure the answer is valid, so if we have
    # processed all the nodes in the template then the current state is a valid
    # question, so add it if it passes our rejection sampling tests.
    if state['next_template_node'] == len(template['nodes']):
      # Use our rejection sampling heuristics to decide whether we should
      # keep this template instantiation
      cur_answer_count = answer_counts[answer]
      answer_counts_sorted = sorted(answer_counts.values())
      median_count = answer_counts_sorted[len(answer_counts_sorted) // 2]
      median_count = max(median_count, 5)

      nb_answers = len(answer_counts_sorted)
      idx = max(int(math.floor(nb_answers*0.15)), 2)


      #std = np.std(answer_counts_sorted, ddof=1)
      # FIXME : Do not hardcode STD threshold
      #if std > 5:
      #  states = reset_states_if_needed(states)
      #  continue

      if cur_answer_count > 1.1 * answer_counts_sorted[-idx]:         # TODO : Those skipping probabilities should be in config file
        if verbose: print('skipping due to second count')
        states = reset_states_if_needed(states)
        continue
      if cur_answer_count > 5.0 * median_count:
        if verbose: print('skipping due to median')
        states = reset_states_if_needed(states)
        continue

      # If the template contains a raw relate node then we need to check for
      # degeneracy at the end
      has_relate = any(n['type'] == 'relate' for n in template['nodes'])
      #has_relate = any(n['type'] == 'relate' for n in q['nodes'])    # FIXME : Relate node is never explicitly in the template. only in the program instantiation.
                                                                      # FIXME : degenerate check is never called
      if has_relate:
        degen = qeng.is_degenerate(q, metadata, scene_struct, answer=answer,
                                   verbose=verbose)
        if degen:
          if verbose: print("Skipping, question is degenerate")
          continue

      answer_counts[answer] += 1
      state['answer'] = answer
      final_states.append(state)
      if max_instances is not None and len(final_states) == max_instances:
        if verbose: print('Breaking out, we got enough instances')
        break
      else:
        states = reset_states_if_needed(states)

      if verbose: print("Added a state to final_states")
      continue

    # Otherwise fetch the next node from the template
    # Make a shallow copy so cached _outputs don't leak ... this is very nasty
    next_node = template['nodes'][state['next_template_node']]
    next_node = question_node_shallow_copy(next_node)

    if next_node['type'] in qeng.functions_to_be_expanded:

      params_in_node = sorted([param_name_to_attribute[i] for i in next_node['side_inputs']])
      
      if next_node['type'].startswith('relate_filter'):
        unique = (next_node['type'] == 'relate_filter_unique')
        not_unique = (next_node['type'] == 'relate_filter_not_unique')
        include_zero = (next_node['type'] == 'relate_filter_count'
                        or next_node['type'] == 'relate_filter_exist')
        filter_options = find_relate_filter_options(answer, scene_struct, params_in_node,
                            template['_can_be_null_attributes'], unique=unique, include_zero=include_zero, not_unique=not_unique)

      else:
        filter_options = find_filter_options(answer, scene_struct, params_in_node, template['_can_be_null_attributes'])
        if next_node['type'] == 'filter':
          # Remove null filter
          # FIXME : There doesn't seem to be a null filter anyways. Should there be one ?
          filter_options.pop((None,) * len(params_in_node), None)

        if next_node['type'] == 'filter_unique':
          single_filter_options = OrderedDict()
          # Get rid of all filter options that don't result in a single object
          for k, v in filter_options.items():
            if len(v) == 1:
              single_filter_options[k] = v
          filter_options = single_filter_options
        elif next_node['type'] == 'filter_not_unique':
          multiple_filter_options = OrderedDict()
          # Get rid of all filter options that don't result in more than one object
          for k, v in filter_options.items():
            if len(v) > 1:
              multiple_filter_options[k] = v
          filter_options = multiple_filter_options
        else:
          # Add some filter options that do NOT correspond to the scene
          if next_node['type'] == 'filter_exist':
            # For filter_exist we want an equal number that do and don't
            num_to_add = len(filter_options)
          elif next_node['type'] == 'filter_count':
            # For filter_count add empty filters equal to the number of singletons
            num_to_add = sum(1 for k, v in filter_options.items() if len(v) == 1)
          else:
            # FIXME : This should never happen, better refactor the code
            num_to_add = 0
          add_empty_filter_options(filter_options, metadata, template['_can_be_null_attributes'], params_in_node, num_to_add)

      # The filter options keys are sorted before being shuffled to control the randomness (ensure reproducibility)
      # This ensure that for the same seed of the random number generator, the same output will be produced
      filter_option_keys = sorted(filter_options.keys(), key=lambda x: x[0] if x[0] is not None else '')

      np.random.shuffle(filter_option_keys)
      for k in filter_option_keys:
        new_nodes = []
        cur_next_vals = {l: v for l, v in state['vals'].items()}
        next_input = state['input_map'][next_node['inputs'][0]]
        filter_side_inputs = sorted(next_node['side_inputs'], key=lambda param: param_name_to_attribute[param])

        if next_node['type'].startswith('relate'):
          param_name = next_node['side_inputs'][0] # First one should be relate   # FIXME : Now that the order of the side inputs doesn't matter, the order of <R> shouldn't either
          filter_side_inputs = sorted(next_node['side_inputs'][1:], key=lambda param: param_name_to_attribute[param])
          param_type = param_name_to_attribute[param_name]
          param_val = k[0]    # Relation value
          k = k[1]            # Other attributes filter
          new_nodes.append({
            'type': 'relate',
            'inputs': [next_input],
            'side_inputs': [param_val],
          })
          cur_next_vals[param_name] = param_val
          next_input = len(state['nodes']) + len(new_nodes) - 1
        for param_name, param_val in zip(filter_side_inputs, k):
          param_type = param_name_to_attribute[param_name]
          filter_type = 'filter_%s' % param_type
          if param_val is not None:
            new_nodes.append({
              'type': filter_type,
              'inputs': [next_input],
              'side_inputs': [param_val],
            })
            cur_next_vals[param_name] = param_val
            next_input = len(state['nodes']) + len(new_nodes) - 1
          else:
            #if param_type == 'instrument':       # FIXME : Hardcoded 'main' attribute. Could be specified in metadata
            #  param_val = 'thing'               # FIXME : Use another name. sound ?
            #else:
            #  param_val = ''
            param_val = ''
            cur_next_vals[param_name] = param_val
        input_map = {k: v for k, v in state['input_map'].items()}
        extra_type = None
        if next_node['type'].endswith('not_unique'):
          extra_type = 'not_unique'
        elif next_node['type'].endswith('unique'):
          extra_type = 'unique'
        elif next_node['type'].endswith('count'):
          extra_type = 'count'
        elif next_node['type'].endswith('exist'):
          extra_type = 'exist'

        if extra_type is not None:
          new_nodes.append({
            'type': extra_type,
            'inputs': [input_map[next_node['inputs'][0]] + len(new_nodes)],
          })
        input_map[state['next_template_node']] = len(state['nodes']) + len(new_nodes) - 1
        states.append({
          'nodes': state['nodes'] + new_nodes,
          'vals': cur_next_vals,
          'input_map': input_map,
          'next_template_node': state['next_template_node'] + 1,
        })

    elif 'side_inputs' in next_node:
      # If the next node has template parameters, expand them out
      # TODO_ORIG: Generalize this to work for nodes with more than one side input
      assert len(next_node['side_inputs']) == 1, 'NOT IMPLEMENTED'

      # Use metadata to figure out domain of valid values for this parameter.
      # Iterate over the values in a random order; then it is safe to bail
      # from the DFS as soon as we find the desired number of valid template
      # instantiations.
      param_name = next_node['side_inputs'][0]
      param_type = param_name_to_attribute[param_name]
      param_vals = metadata['attributes'][param_type]['values'][:]
      np.random.shuffle(param_vals)
      for val in param_vals:
        input_map = {k: v for k, v in state['input_map'].items()}
        input_map[state['next_template_node']] = len(state['nodes'])
        cur_next_node = {
          'type': next_node['type'],
          'inputs': [input_map[idx] for idx in next_node['inputs']],
          'side_inputs': [val],
        }
        cur_next_vals = {k: v for k, v in state['vals'].items()}
        cur_next_vals[param_name] = val

        states.append({
          'nodes': state['nodes'] + [cur_next_node],
          'vals': cur_next_vals,
          'input_map': input_map,
          'next_template_node': state['next_template_node'] + 1,
        })
    else:
      input_map = {k: v for k, v in state['input_map'].items()}
      input_map[state['next_template_node']] = len(state['nodes'])
      next_node = {
        'type': next_node['type'],
        'inputs': [input_map[idx] for idx in next_node['inputs']],
      }
      states.append({
        'nodes': state['nodes'] + [next_node],
        'vals': state['vals'],
        'input_map': input_map,
        'next_template_node': state['next_template_node'] + 1,
      })

  # Actually instantiate the template with the solutions we've found
  return instantiate_texts_from_solutions(template, synonyms, final_states)


# TODO : Adapt other_heuristic
def other_heuristic(text, param_vals):
  """
  Post-processing heuristic to handle the word "other"
  """
  if ' other ' not in text and ' another ' not in text:
    return text
  target_keys = {
    '<Z>',  '<C>',  '<M>',  '<S>',        # FIXME : Hardcoded string placeholder
    '<Z2>', '<C2>', '<M2>', '<S2>',
  }
  if param_vals.keys() != target_keys:
    return text
  key_pairs = [
    ('<Z>', '<Z2>'),                      # FIXME : Hardcoded string placeholder
    ('<C>', '<C2>'),
    ('<M>', '<M2>'),
    ('<S>', '<S2>'),
  ]
  remove_other = False
  for k1, k2 in key_pairs:
    v1 = param_vals.get(k1, None)
    v2 = param_vals.get(k2, None)
    if v1 != '' and v2 != '' and v1 != v2:
      print('other has got to go! %s = %s but %s = %s'
            % (k1, v1, k2, v2))
      remove_other = True
      break
  if remove_other:
    if ' other ' in text:
      text = text.replace(' other ', ' ')
    if ' another ' in text:
      text = text.replace(' another ', ' a ')
  return text


def load_and_prepare_templates(template_dir):
  # Load templates from disk
  # Key is (filename, file_idx)

  num_loaded_templates = 0
  templates = OrderedDict()
  for fn in os.listdir(template_dir):
    if not fn.endswith('.json'): continue
    with open(os.path.join(template_dir, fn), 'r') as f:
      try:
        template_json = ujson.load(f)
        for i, template in enumerate(template_json):
          num_loaded_templates += 1
          key = (fn, i)

          # Adding optionals parameters if not present. Remove the need to do null check when accessing
          optionals_keys = ['constraints', 'can_be_null_attributes']
          for op_key in optionals_keys:
            if op_key not in template:
              template[op_key] = []

          templates[key] = template
      except ValueError:
        print("[ERROR] Could not load template %s" % fn)    # FIXME : We should probably pause or do something to inform the user. This message will be flooded by the rest of the output. Maybe do a pause before generating ?
  print('Read %d templates from disk' % num_loaded_templates)

  return templates


def load_scenes(scene_filepath, start_idx, nb_scenes_to_gen):
  # Read file containing input scenes
  with open(scene_filepath, 'r') as f:
    scene_data = ujson.load(f)
    scenes = scene_data['scenes']
    nb_scenes_loaded = len(scenes)
    scene_info = scene_data['info']

  if nb_scenes_to_gen > 0:
    end = start_idx + nb_scenes_to_gen
    end = end if end < nb_scenes_loaded else nb_scenes_loaded
    scenes = scenes[start_idx:end]
  else:
    scenes = scenes[start_idx:]

  print('Read %d scenes from disk' % len(scenes))

  return scenes, scene_info


def load_and_prepare_metadata(metadata_filepath, scenes):
  # Loading metadata
  with open(metadata_filepath, 'r') as f:
    metadata = ujson.load(f)

  # To initialize the metadata, we first need to know how many instruments each scene contains
  instrument_count_empty = {}
  instrument_indexes_empty = {}

  for instrument in metadata['attributes']['instrument']['values']:
    instrument_count_empty[instrument] = 0
    instrument_indexes_empty[instrument] = []

  instrument_count = dict(instrument_count_empty)

  max_scene_length = 0
  for scene in scenes:
    # Keep track of the maximum number of objects across all scenes
    scene_length = len(scene['objects'])
    if scene_length > max_scene_length:
      max_scene_length = scene_length

    # Keep track of the indexes for each instrument
    instrument_indexes = copy.deepcopy(instrument_indexes_empty)
    for i, obj in enumerate(scene['objects']):
      instrument_indexes[obj['instrument']].append(i)

    # TODO : Generalize this for all attributes
    # Insert the instrument indexes in the scene definition
    # (Will be used for relative positioning. Increased performance compared to doing the search everytime)
    scene['instrument_indexes'] = instrument_indexes

    # Retrieve the maximum number of occurence for each instruments
    for instrument, index_list in instrument_indexes.items():
      count = len(index_list)
      if count > instrument_count[instrument]:
        instrument_count[instrument] = count

    # Insert reference from relation label (Ex: before, after, ..) to index in scene['relationships']
    # Again for performance. Faster than searching in the dict every time
    scene['_relationships_indexes'] = {}
    for i, relation_data in enumerate(scene['relationships']):
      scene['_relationships_indexes'][relation_data['type']] = i

  # Instantiate the question engine attributes handlers
  qeng.instantiate_attributes_handlers(metadata, instrument_count, max_scene_length)

  return metadata, max_scene_length


def create_reset_counts_fct(templates, metadata, max_scene_length):
  def reset_counts():
    # Maps a template (filename, index) to the number of questions we have
    # so far using that template
    template_counts = {}
    # Maps a template (filename, index) to a dict mapping the answer to the
    # number of questions so far of that template type with that answer
    template_answer_counts = {}
    for key, template in templates.items():
      template_counts[key] = 0
      last_node = template['nodes'][-1]['type']
      output_type = qeng.functions[last_node]['output']

      if output_type == 'bool':
        answers = [True, False]
      elif output_type == 'integer':
        answers = list(range(0, max_scene_length + 1))      # FIXME : This won't hold if the scenes have different length
      else:
        answers = metadata['attributes'][output_type]['values']

      template_answer_counts[key] = {}
      for a in answers:
        template_answer_counts[key][a] = 0
    return template_counts, template_answer_counts

  return reset_counts


def load_synonyms(synonyms_filepath):
  # Read synonyms file
  with open(args.synonyms_json, 'r') as f:
    return ujson.load(f)


def main(args):
  # Paths definition from arguments
  experiment_output_folder = os.path.join(args.output_folder, args.output_version_nb)
  questions_output_folder = os.path.join(experiment_output_folder, 'questions')
  tmp_output_folder = os.path.join(questions_output_folder, 'TMP_%s' % args.set_type)
  questions_filename = '%s_%s_questions.json' % (args.output_filename_prefix, args.set_type)
  questions_output_filepath = os.path.join(questions_output_folder, questions_filename)
  scene_filepath = os.path.join(experiment_output_folder, 'scenes',
                                '%s_%s_scenes.json' % (args.output_filename_prefix, args.set_type))

  # Setting the random seed from arguments
  if args.random_nb_generator_seed is not None:
    init_random_seed(args.random_nb_generator_seed)
  else:
    print("The seed must be specified in the arguments.", file=sys.stderr)
    exit(1)

  # Folder structure creation
  if not os.path.isdir(experiment_output_folder):
    os.mkdir(experiment_output_folder)

  if not os.path.isdir(questions_output_folder):
    os.mkdir(questions_output_folder)

  question_file_exist = os.path.isfile(questions_output_filepath)
  if question_file_exist and args.clear_existing_files:
    os.remove(questions_output_filepath)
  elif question_file_exist:
    print("This experiment have already been run. Please bump the version number or delete the previous output.",
          file=sys.stderr)
    exit(1)

  # Create tmp folder to store questions (separated in small files)
  if not os.path.isdir(tmp_output_folder):
    os.mkdir(tmp_output_folder)
  elif args.clear_existing_files:
    rm_dir(tmp_output_folder)
    os.mkdir(tmp_output_folder)
  else:
    print("Directory %s already exist. Please change the output filename", file=sys.stderr)
    exit(1)  # FIXME : Maybe we should have a prompt ? This might be dangerous while running experiments automatically. We might get stuck there and waste a lot of time

  # Load templates, scenes, metadata and synonyms from file
  scenes, scene_info = load_scenes(scene_filepath, args.scene_start_idx, args.num_scenes)   # FIXME : Get rid of scene_info
  metadata, max_scene_length = load_and_prepare_metadata(args.metadata_file, scenes)
  templates = load_and_prepare_templates(args.template_dir)
  synonyms = load_synonyms(args.synonyms_json)

  # Helper function
  reset_counts = create_reset_counts_fct(templates, metadata, max_scene_length)

  # Initialisation
  questions = []
  question_index = 0
  file_written = 0
  scene_count = 0
  nb_scenes = len(scenes)

  for i, scene in enumerate(scenes):
    scene_fn = scene['image_filename']          # FIXME : Scene key related to image. Should refer to "sound_filename" or scene
    scene_struct = scene
    print('starting scene %s (%d / %d)' % (scene_fn, i + 1, nb_scenes))

    if scene_count % args.reset_counts_every == 0:
      template_counts, template_answer_counts = reset_counts()
    scene_count += 1

    # Order templates by the number of questions we have so far for those
    # templates. This is a simple heuristic to give a flat distribution over templates.
    # We shuffle the templates before sorting to ensure variability when the counts are equals
    templates_items = list(templates.items())
    np.random.shuffle(templates_items)
    templates_items = sorted(templates_items,
                        key=lambda x: template_counts[x[0]])

    num_instantiated = 0
    for (template_fn, template_idx), template in templates_items:
      if 'disabled' in template and template['disabled']:
        continue

      print('    trying template ', template_fn, template_idx, flush=True)

      ts, qs, ans = instantiate_templates_dfs(
                      scene_struct,
                      template,
                      metadata,
                      template_answer_counts[(template_fn, template_idx)],
                      synonyms,
                      reset_threshold=args.instantiation_retry_threshold,
                      max_instances=args.instances_per_template,
                      verbose=args.verbose)
      image_index = int(os.path.splitext(scene_fn)[0].split('_')[-1])
      for t, q, a in zip(ts, qs, ans):
        questions.append({
          'split': args.set_type,
          'image_filename': scene_fn,            # FIXME : Do we even need this ? We can reconstruct from the image index & prefix
          'image_index': image_index,            # FIXME : should be scene index
          'image': os.path.splitext(scene_fn)[0],
          'question': t,
          'program': q,
          'answer': a,
          'template_filename': '%s-%d' % (template_fn, template_idx),   # FIXME : This should be template_id
          'question_family_index': template_idx,         # FIXME : This index doesn't represent the question family index
          'question_index': question_index,     # FIXME : This is not efficient
        })
        question_index += 1
      if len(ts) > 0:
        num_instantiated += 1
        template_counts[(template_fn, template_idx)] += 1
      elif args.verbose:
        print('Could not generate any question for template "%s-%d"' % (template_fn, template_idx))

      if num_instantiated >= args.templates_per_image:
        # We have instantiated enough template for this scene
        break

    if question_index != 0 and question_index % args.write_to_file_every == 0:
      write_questions_part_to_file(tmp_output_folder, questions_filename, scene_info, questions, file_written)    # FIXME : Remove the scene_info
      file_written += 1
      questions = []

  if len(questions) > 0 or file_written == 0:
    # Write the rest of the questions
    # If no file were written and we have 0 questions, 
    # we still want an output file with no questions (Otherwise it will break the pipeline)
    write_questions_part_to_file(tmp_output_folder, questions_filename, scene_info, questions, file_written)      # FIXME : Remove the scene_info


if __name__ == '__main__':
  args = parser.parse_args()
  main(args)

