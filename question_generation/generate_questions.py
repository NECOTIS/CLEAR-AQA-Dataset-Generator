# TODO : Add author mention
# TODO : Change heading comments
# FIXME : A lot of info are retrieved from metadata. They don't change for each instantiations. We should only retrieve them once instead of everytime

# Copyright 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree. An additional grant
# of patent rights can be found in the PATENTS file in the same directory.

from __future__ import print_function
import argparse, ujson, os, random, math
import time
import re

import question_engine as qeng

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

# TODO : Change defaults values for arguments
# TODO : Change argument text
# Inputs
parser.add_argument('--input_scene_file', default='../output/CLEVR_scenes.json',
    help="JSON file containing ground-truth scene information for all images " +
         "from render_images.py")
parser.add_argument('--metadata_file', default='metadata.json',
    help="JSON file containing metadata about functions")
parser.add_argument('--synonyms_json', default='synonyms.json',
    help="JSON file defining synonyms for parameter values")
parser.add_argument('--template_dir', default='CLEVR_1.0_templates',
    help="Directory containing JSON templates for questions")

# Output
parser.add_argument('--output_questions_file',
    default='../output/CLEVR_questions.json',
    help="The output file to write containing generated questions")

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

# Misc
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
# args = parser.parse_args()


def precompute_filter_options(scene_struct, attr_keys, allow_empty_instrument=False):
  # Keys are tuples (size, color, shape, material) (where some may be None)
  # and values are lists of object idxs that match the filter criterion
  attribute_map = {}

  # Precompute masks
  masks = []
  for i in range(2 ** len(attr_keys)):
    mask = []
    for j in range(len(attr_keys)):
      mask.append((i // (2 ** j)) % 2)
    masks.append(mask)

  for object_idx, obj in enumerate(scene_struct['objects']):
    keys = [tuple(obj[k] for k in attr_keys)]

    for mask in masks:
      for key in keys:
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

  # FIXME : This is not efficient, we should strip those options at the beginning
  if not allow_empty_instrument and "instrument" in attr_keys:
    instument_key_index = attr_keys.index("instrument")     # FIXME : This should probably not be hardcoded. Use config file ?
    for key in list(attribute_map.keys()):
      # Remove all the filters which contain empty instruments
      if key[instument_key_index] is None:
        del attribute_map[key]

  if not '_filter_options' in scene_struct:
    scene_struct['_filter_options'] = {}

  scene_struct['_filter_options'][tuple(attr_keys)] = attribute_map


def find_filter_options(object_idxs, scene_struct, attr):
  # Keys are tuples (size, color, shape, material) (where some may be None)
  # and values are lists of object idxs that match the filter criterion
  filter_key = tuple(attr)

  if '_filter_options' not in scene_struct or filter_key not in scene_struct['_filter_options']:
    precompute_filter_options(scene_struct, attr)

  attribute_map = {}
  object_idxs = set(object_idxs)
  for k, vs in scene_struct['_filter_options'][filter_key].items():
    attribute_map[k] = sorted(list(object_idxs & vs))
  return attribute_map


def add_empty_filter_options(attribute_map, metadata, attr_keys, num_to_add):
  # Add some filtering criterion that do NOT correspond to objects

  # FIXME : This is somewhat hackish...
  instrument_index = attr_keys.index("instrument")
  attr_vals = [metadata['attributes'][t]['values'] + [None] for t in attr_keys]

  attr_vals[instrument_index].remove(None)

  target_size = len(attribute_map) + num_to_add
  counter = 0
  while len(attribute_map) < target_size:
    k = tuple(random.choice(v) for v in attr_vals)
    if k not in attribute_map:
      attribute_map[k] = []
    else:
      # FIXME : This is an ugly patch. we should specify a valid num_to_add instead
      if counter > 100:
        print("Reached empty filter treshold.")
        break
      counter += 1


def find_relate_filter_options(object_idx, scene_struct, attr,
    unique=False, include_zero=False, trivial_frac=0.1):
  options = {}

  attr = [a for a in attr if not a.startswith('relate')]
  filter_key = tuple(attr)

  if '_filter_options' not in scene_struct or filter_key not in scene_struct['_filter_options']:
    precompute_filter_options(scene_struct, attr)

  # TODO/VERIFY : Will probably have to change the definition of "trivial"
  # TODO_ORIG: Right now this is only looking for nontrivial combinations; in some
  # cases I may want to add trivial combinations, either where the intersection
  # is empty or where the intersection is equal to the filtering output.
  trivial_options = {}
  for relationship in scene_struct['relationships']:
    related = set(scene_struct['relationships'][relationship][object_idx])
    for filters, filtered in scene_struct['_filter_options'][filter_key].items():
      intersection = related & filtered
      trivial = (intersection == filtered)
      if unique and len(intersection) != 1: continue
      if not include_zero and len(intersection) == 0: continue
      if trivial:
        trivial_options[(relationship, filters)] = sorted(list(intersection))
      else:
        options[(relationship, filters)] = sorted(list(intersection))

  N, f = len(options), trivial_frac
  num_trivial = int(round(N * f / (1 - f)))
  trivial_options = list(trivial_options.items())
  random.shuffle(trivial_options)
  for k, v in trivial_options[:num_trivial]:
    options[k] = v

  return options


def node_shallow_copy(node):
  new_node = {
    'type': node['type'],
    'inputs': node['inputs'],
  }
  if 'side_inputs' in node:
    new_node['side_inputs'] = node['side_inputs']
  return new_node

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


def placeholders_to_attribute(template_text,metadata):
  correspondences = {}
  # Extracting the placeholders from the text
  reg = re.compile('<([a-zA-Z]+)(\d)?>')
  matches = re.findall(reg,template_text)

  # FIXME : By iterating over each types, we also iterate over relation. Do we want this ?
  attribute_correspondences = {metadata['attributes'][t]['placeholder']: t for t in metadata['attributes']}

  for placeholder in matches:
    correspondences['<%s%s>' % (placeholder[0], placeholder[1])] = attribute_correspondences['<%s>' % placeholder[0]]

  return correspondences


def generate_not_null_instrument_constraints(template_text):
  reg = re.compile('<I\d?>')
  matches = re.findall(reg, template_text)

  constraints = []

  for matche in matches:
    constraints.append({
      "params" : [matche],
      "type" : "NOT_NULL"
    })

  return constraints


def instantiate_templates_dfs(scene_struct, template, metadata, answer_counts,
                              synonyms, max_instances=None, verbose=False):

  param_name_to_attribute = placeholders_to_attribute(template['text'][0], metadata)

  initial_state = {
    'nodes': [node_shallow_copy(template['nodes'][0])],
    'vals': {},
    'input_map': {0: 0},
    'next_template_node': 1,
  }
  states = [initial_state]
  final_states = []
  while states:
    state = states.pop()

    # Check to make sure the current state is valid
    q = {'nodes': state['nodes']}
    outputs = qeng.answer_question(q, metadata, scene_struct, all_outputs=True)
    answer = outputs[-1]
    if answer == '__INVALID__':
      if verbose: print("Skipping due to invalid answer")
      continue

    # Check to make sure constraints are satisfied for the current state
    skip_state = False
    for constraint in template['constraints']:
      if constraint['type'] == 'NEQ':
        p1, p2 = constraint['params']
        v1, v2 = state['vals'].get(p1), state['vals'].get(p2)
        if v1 is not None and v2 is not None and v1 == v2:
          if verbose:
            print('skipping due to NEQ constraint')
            print(constraint)
            print(state['vals'])
          skip_state = True
          break
      elif constraint['type'] == 'NULL':
        p = constraint['params'][0]
        p_type = param_name_to_attribute[p]
        v = state['vals'].get(p)
        if v is not None:
          skip = False
          if p_type == 'instrument' and v != 'thing': skip = True        # FIXME : Hardcoded stuff here
          if p_type != 'instrument' and v != '': skip = True
          if skip:
            if verbose:
              print('skipping due to NULL constraint')
              print(constraint)
              print(state['vals'])
            skip_state = True
            break
      elif constraint['type'] == 'NOT_NULL':
        p = constraint['params'][0]
        p_type = param_name_to_attribute[p]
        v = state['vals'].get(p)
        if v is not None and (v == '' or v =='thing'):
          skip = True     # FIXME : Ugly
          if skip:
            if verbose:
              print('skipping due to NOT NULL constraint')
              print(constraint)
              print(state['vals'])
            skip_state = True
            break
      elif constraint['type'] == 'OUT_NEQ':
        i, j = constraint['params']
        i = state['input_map'].get(i, None)
        j = state['input_map'].get(j, None)
        if i is not None and j is not None and outputs[i] == outputs[j]:
          if verbose:
            print('skipping due to OUT_NEQ constraint')
          skip_state = True
          break
      else:
        assert False, 'Unrecognized constraint type "%s"' % constraint['type']

    if skip_state:
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
      if cur_answer_count > 1.1 * answer_counts_sorted[-2]:         # TODO : Those skipping probabilities should be in config file
        if verbose: print('skipping due to second count')
        continue
      if cur_answer_count > 5.0 * median_count:
        if verbose: print('skipping due to median')
        continue

      # If the template contains a raw relate node then we need to check for
      # degeneracy at the end
      has_relate = any(n['type'] == 'relate' for n in template['nodes'])
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
      if verbose: print("Added a state to final_states")
      continue

    # Otherwise fetch the next node from the template
    # Make a shallow copy so cached _outputs don't leak ... this is very nasty
    next_node = template['nodes'][state['next_template_node']]
    next_node = node_shallow_copy(next_node)

    special_nodes = {                                             # FIXME : Some harcoded special node
        'filter_unique', 'filter_count', 'filter_exist', 'filter',
        'relate_filter', 'relate_filter_unique', 'relate_filter_count',
        'relate_filter_exist',
    }
    if next_node['type'] in special_nodes:

      params_in_node = sorted([param_name_to_attribute[i] for i in next_node['side_inputs']])
      
      if next_node['type'].startswith('relate_filter'):
        unique = (next_node['type'] == 'relate_filter_unique')
        include_zero = (next_node['type'] == 'relate_filter_count'
                        or next_node['type'] == 'relate_filter_exist')
        filter_options = find_relate_filter_options(answer, scene_struct, params_in_node,
                            unique=unique, include_zero=include_zero)
      else:
        filter_options = find_filter_options(answer, scene_struct, params_in_node)
        if next_node['type'] == 'filter':
          # Remove null filter
          # FIXME : There doesn't seem to be a null filter anyways. Should there be one ?
          filter_options.pop((None,) * len(params_in_node), None)

        if next_node['type'] == 'filter_unique':
          # Get rid of all filter options that don't result in a single object
          filter_options = {k: v for k, v in filter_options.items() if len(v) == 1}
        else:
          # Add some filter options that do NOT correspond to the scene
          if next_node['type'] == 'filter_exist':
            # For filter_exist we want an equal number that do and don't
            num_to_add = len(filter_options)
          elif next_node['type'] == 'filter_count' or next_node['type'] == 'filter':
            # For filter_count add nulls equal to the number of singletons
            num_to_add = sum(1 for k, v in filter_options.items() if len(v) == 1)
          else:
            # FIXME : This should never happen, better refactor the code
            num_to_add = 0
          add_empty_filter_options(filter_options, metadata, params_in_node, num_to_add)

      filter_option_keys = list(filter_options.keys())
      random.shuffle(filter_option_keys)
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
        if next_node['type'].endswith('unique'):
          extra_type = 'unique'
        if next_node['type'].endswith('count'):
          extra_type = 'count'
        if next_node['type'].endswith('exist'):
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
      random.shuffle(param_vals)
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
  text_questions, structured_questions, answers = [], [], []
  for state in final_states:
    structured_questions.append(state['nodes'])
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


# FIXME : The probability should be loaded from config
def replace_optionals(s):
  """
  Each substring of s that is surrounded in square brackets is treated as
  optional and is removed with probability 0.5. For example the string

  "A [aa] B [bb]"

  could become any of

  "A aa B bb"
  "A  B bb"
  "A aa B "
  "A  B "

  with probability 1/4.
  """
  pat = re.compile(r'\[([^\[]*)\]')

  while True:
    match = re.search(pat, s)
    if not match:
      break
    i0 = match.start()
    i1 = match.end()
    if random.random() > 0.5:
      s = s[:i0] + match.groups()[0] + s[i1:]
    else:
      s = s[:i0] + s[i1:]
  return s


def global_position_from_index(idx, nb_obj):
  """
  Separate the scene in 3 parts (Beginning, middle, end) based on the index of the object
  """

  # FIXME : Handle correctly whe nthe number of objects is not a multiple of 3
  part_size = math.floor(nb_obj/3)
  if idx + 1 <= part_size:
    return "beginning"
  elif idx + 1 <= 2*part_size:
    return "middle"
  else:
    return "end"

def main(args):
  with open(args.metadata_file, 'r') as f:
    metadata = ujson.load(f)
  
  functions_by_name = {}
  for f in metadata['functions']:
    functions_by_name[f['name']] = f
  metadata['_functions_by_name'] = functions_by_name

  # Load templates from disk
  # Key is (filename, file_idx)
  num_loaded_templates = 0
  templates = {}
  for fn in os.listdir(args.template_dir):
    if not fn.endswith('.json'): continue
    with open(os.path.join(args.template_dir, fn), 'r') as f:
      base = os.path.splitext(fn)[0]
      try:
        template_json = ujson.load(f)
        for i, template in enumerate(template_json):
          num_loaded_templates += 1
          key = (fn, i)
          templates[key] = template
      except ValueError:
        print("Could not load template %s" % fn)    # FIXME : We should probably pause or do something to inform the user. This message will be flooded by the rest of the output. Maybe do a pause before generating ?
  print('Read %d templates from disk' % num_loaded_templates)

  # TODO : Handle augmentedScene attributes (Position)
  def reset_counts():
    # Maps a template (filename, index) to the number of questions we have
    # so far using that template
    template_counts = {}
    # Maps a template (filename, index) to a dict mapping the answer to the
    # number of questions so far of that template type with that answer
    template_answer_counts = {}
    node_type_to_dtype = {n['name']: n['output'] for n in metadata['functions']}
    for key, template in templates.items():
      template_counts[key] = 0
      final_node_type = template['nodes'][-1]['type']
      final_dtype = node_type_to_dtype[final_node_type]

      if final_dtype == 'bool':
        answers = [True, False]
      elif final_dtype == 'integer':
        answers = list(range(0, 10))              # FIXME : Max integer anser should be loaded from config
      else:
        answers = metadata['attributes'][final_dtype]['values']

      template_answer_counts[key] = {}
      for a in answers:
        template_answer_counts[key][a] = 0      # FIXME/INVESTIGATE : What happend when the answers is Null/None ? This will happen with Object and ObjectSet
    return template_counts, template_answer_counts

  template_counts, template_answer_counts = reset_counts()

  # Read file containing input scenes
  all_scenes = []
  with open(args.input_scene_file, 'r') as f:
    scene_data = ujson.load(f)
    all_scenes = scene_data['scenes']
    scene_info = scene_data['info']
  begin = args.scene_start_idx
  if args.num_scenes > 0:
    end = args.scene_start_idx + args.num_scenes
    all_scenes = all_scenes[begin:end]
  else:
    all_scenes = all_scenes[begin:]

  # Read synonyms file
  with open(args.synonyms_json, 'r') as f:
    synonyms = ujson.load(f)

  questions = []
  scene_count = 0
  for i, scene in enumerate(all_scenes):
    scene_fn = scene['image_filename']          # FIXME : Scene key related to image. Should refer to "sound_filename" or scene
    scene_struct = scene
    print('starting image %s (%d / %d)'
          % (scene_fn, i + 1, len(all_scenes)))

    if scene_count % args.reset_counts_every == 0:
      print('resetting counts')
      template_counts, template_answer_counts = reset_counts()
    scene_count += 1

    # Order templates by the number of questions we have so far for those
    # templates. This is a simple heuristic to give a flat distribution over
    # templates.
    templates_items = list(templates.items())
    templates_items = sorted(templates_items,
                        key=lambda x: template_counts[x[0]])
    num_instantiated = 0
    for (fn, idx), template in templates_items:
      if 'disabled' in template and template['disabled']:
        continue
      if args.verbose:
        print('trying template ', fn, idx)
      if args.time_dfs and args.verbose:
        tic = time.time()
      ts, qs, ans = instantiate_templates_dfs(
                      scene_struct,
                      template,
                      metadata,
                      template_answer_counts[(fn, idx)],
                      synonyms,
                      max_instances=args.instances_per_template,
                      verbose=args.verbose)
      if args.time_dfs and args.verbose:
        toc = time.time()
        print('that took ', toc - tic)
      image_index = int(os.path.splitext(scene_fn)[0].split('_')[-1])
      for t, q, a in zip(ts, qs, ans):
        questions.append({
          'split': scene_info['split'],
          'image_filename': scene_fn,
          'image_index': image_index,
          'image': os.path.splitext(scene_fn)[0],
          'question': t,
          'program': q,
          'answer': a,
          'template_filename': fn,
          'question_family_index': idx,
          'question_index': len(questions),
        })
      if len(ts) > 0:
        if args.verbose:
          print('got one!')
        num_instantiated += 1
        template_counts[(fn, idx)] += 1
      elif args.verbose:
        print('did not get any =(')
      if num_instantiated >= args.templates_per_image:
        break

  # Change "side_inputs" to "value_inputs" in all functions of all functional         # FIXME : Fix this at the same time
  # programs. My original name for these was "side_inputs" but I decided to
  # change the name to "value_inputs" for the public CLEVR release. I should
  # probably go through all question generation code and templates and rename,
  # but that could be tricky and take a while, so instead I'll just do it here.
  # To further complicate things, originally functions without value inputs did
  # not have a "side_inputs" field at all, and I'm pretty sure this fact is used
  # in some of the code above; however in the public CLEVR release all functions
  # have a "value_inputs" field, and it's an empty list for functions that take
  # no value inputs. Again this should probably be refactored, but the quick and
  # dirty solution is to keep the code above as-is, but here make "value_inputs"
  # an empty list for those functions that do not have "side_inputs". Gross.
  for q in questions:
    for f in q['program']:
      if 'side_inputs' in f:
        f['value_inputs'] = f['side_inputs']
        del f['side_inputs']
      else:
        f['value_inputs'] = []

  with open(args.output_questions_file, 'w') as f:
    print('Writing output to %s' % args.output_questions_file)
    ujson.dump({
        'info': scene_info,
        'questions': questions,
      }, f, escape_forward_slashes=False)


if __name__ == '__main__':
  args = parser.parse_args()
  if args.profile:
    import cProfile
    cProfile.run('main(args)')
  else:
    main(args)

